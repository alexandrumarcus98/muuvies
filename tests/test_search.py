from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app


@pytest_asyncio.fixture(autouse=True)
async def setup_search_db():
    await init_db()


@pytest.mark.asyncio
async def test_search_filters_linkless_dmca_stub_torrents():
    mock_prowlarr_results = [
        {
            "guid": "linkless-stub-1",
            "title": "Dune.Part.Two.2024.2160p.REMUX-DMCA-STUB",
            "category": "Movies",
            "indexer": "1337x",
            "size": 75_000_000_000,
            "size_formatted": "69.85 GB",
            "seeders": 900,
            "leechers": 10,
            "download_url": None,
            "magnet_url": None,
            "info_hash": None,
            "is_adult": False,
        },
        {
            "guid": "valid-release-1",
            "title": "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.Atmos-Framestor",
            "category": "Movies",
            "indexer": "PrivateMovies",
            "size": 82_000_000_000,
            "size_formatted": "76.37 GB",
            "seeders": 120,
            "leechers": 4,
            "download_url": "http://seedbox-tunnel:9696/prowlarr/1/download?apikey=x",
            "magnet_url": None,
            "info_hash": "DEADBEEF1234",
            "is_adult": False,
        },
        {
            "guid": "valid-magnet-2",
            "title": "Dune.Part.Two.2024.1080p.WEB-DL-NTb",
            "category": "Movies",
            "indexer": "Knaben",
            "size": 12_000_000_000,
            "size_formatted": "11.18 GB",
            "seeders": 45,
            "leechers": 2,
            "download_url": None,
            "magnet_url": "magnet:?xt=urn:btih:CAFEBABE5678",
            "info_hash": None,
            "is_adult": False,
        },
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        with patch(
            "backend.app.services.prowlarr.prowlarr_client.search",
            new=AsyncMock(return_value=mock_prowlarr_results),
        ):
            res = await client.post(
                "/api/search",
                json={"query": "DuneLinklessFilterTest", "category": "Movies", "ai_smart_search": False},
            )
            assert res.status_code == 200
            data = res.json()
            guids = [r["guid"] for r in data["results"]]
            assert "linkless-stub-1" not in guids
            assert set(guids) == {"valid-release-1", "valid-magnet-2"}
            assert all(r["can_grab"] is True for r in data["results"])


@pytest.mark.asyncio
async def test_prowlarr_partial_results_and_4k_2160p_expansion(monkeypatch):
    import httpx
    from unittest.mock import MagicMock
    from backend.app.services.prowlarr import ProwlarrClient, expand_resolution_queries

    assert expand_resolution_queries("sumiko smile 4k") == ["sumiko smile 4k", "sumiko smile 2160p"]
    assert expand_resolution_queries("sumiko smile 2160p") == ["sumiko smile 2160p", "sumiko smile 4K"]

    client = ProwlarrClient()
    indexers_payload = [
        {"id": 3, "name": "1337x", "enable": True, "capabilities": {"categories": [{"id": 6000}]}},
        {"id": 9, "name": "Knaben", "enable": True, "capabilities": {"categories": [{"id": 6000}]}},
        {"id": 7, "name": "YTS", "enable": True, "capabilities": {"categories": [{"id": 2000}]}},
    ]

    async def fake_get(url, headers=None, params=None, timeout=None):
        if url.endswith("/api/v1/indexer"):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = indexers_payload
            return resp

        idx_id = (params or {}).get("indexerIds")
        q = (params or {}).get("query")
        if idx_id == 3:
            raise httpx.ReadTimeout("1337x timed out")
        resp = MagicMock()
        resp.status_code = 200
        if idx_id == 9 and q == "sumiko smile 2160p":
            resp.json.return_value = [
                {
                    "guid": "knaben-1",
                    "title": "Sumiko.Smile.2160p.MP4",
                    "categories": [{"id": 6000, "name": "XXX"}],
                    "indexer": "Knaben",
                    "size": 8_000_000_000,
                    "seeders": 12,
                    "leechers": 1,
                    "infoHash": "AABBCC112233",
                    "magnetUrl": "magnet:?xt=urn:btih:AABBCC112233",
                }
            ]
        elif idx_id == 9 and q == "sumiko smile 4k":
            resp.json.return_value = []
        else:
            resp.json.return_value = []
        return resp

    mock_http = MagicMock()
    mock_http.get = AsyncMock(side_effect=fake_get)
    monkeypatch.setattr("backend.app.services.prowlarr.get_search_client", lambda: mock_http)

    # Vault locked must block Adult immediately
    assert await client.search("sumiko smile 4k", category="Adult", vault_unlocked=False) == []

    # Vault unlocked preserves partial results from Knaben even though 1337x timed out,
    # and skips YTS (Movies-only).
    results = await client.search("sumiko smile 4k", category="Adult", vault_unlocked=True)
    assert len(results) == 1
    assert results[0]["guid"] == "knaben-1"
    assert results[0]["indexer"] == "Knaben"
    assert results[0]["is_adult"] is True


@pytest.mark.asyncio
async def test_4k_query_heuristic_ranking_and_empty_cache_guard():
    from backend.app.ai_assistant import ai_assistant
    from backend.app.services.cache import search_cache

    search_cache.clear()
    enriched, summary = ai_assistant.enrich_and_score_heuristically(
        query="sumiko smile 2160p",
        releases=[
            {
                "guid": "hd-high-seed",
                "title": "Sumiko.Smile.1080p.WEB-DL",
                "size": 2_000_000_000,
                "size_formatted": "1.86 GB",
                "seeders": 50,
                "indexer": "Knaben",
            },
            {
                "guid": "uhd-pick",
                "title": "Sumiko.Smile.2160p.MP4",
                "size": 8_000_000_000,
                "size_formatted": "7.45 GB",
                "seeders": 8,
                "indexer": "VaultGallery",
            },
        ],
        library_items=[],
    )
    assert summary is not None
    assert summary.recommended_guid == "uhd-pick"

    # Transient empty search result must not poison search_cache
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        with patch(
            "backend.app.services.prowlarr.prowlarr_client.search",
            new=AsyncMock(return_value=[]),
        ):
            r = await client.post(
                "/api/search",
                json={"query": "TransientEmptyQuery", "category": "Movies", "ai_smart_search": False},
            )
            assert r.status_code == 200
            assert r.json()["results"] == []
            assert search_cache.get("TransientEmptyQuery|movies|v0") is None


@pytest.mark.asyncio
async def test_search_does_not_call_llm_and_ai_pick_curates_on_demand():
    from backend.app.ai_assistant import ai_assistant
    from backend.app.schemas import SmartRankingSummary
    from backend.app.services.cache import curator_cache, search_cache

    search_cache._cache.clear()
    curator_cache.clear()
    mock_releases = [
        {
            "guid": "rel-1",
            "title": "Dune.2021.2160p.WEB-DL.AAC.MP4",
            "category": "Movies",
            "indexer": "PrivateTracker",
            "size": 19_000_000_000,
            "size_formatted": "17.69 GB",
            "seeders": 95,
            "leechers": 2,
            "download_url": "https://private-tracker.example/dl/dune",
            "magnet_url": None,
            "info_hash": "a" * 40,
        }
    ]
    fake_summary = SmartRankingSummary(
        ai_active=True,
        query_intent="On-demand pick",
        recommended_guid="rel-1",
        recommended_title="Dune.2021.2160p.WEB-DL.AAC.MP4",
        recommendation_reason="Selected on demand via AI Pick.",
        model_used="Gemini Test",
    )
    mock_curate = AsyncMock(return_value=(fake_summary, {"rel-1": "Selected on demand"}, False))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        with (
            patch("backend.app.services.prowlarr.prowlarr_client.search", new=AsyncMock(return_value=mock_releases)),
            patch.object(ai_assistant, "curate_top_candidates", mock_curate),
        ):
            search_res = await client.get("/api/search?q=DuneOnDemandTest&category=Movies")
            assert search_res.status_code == 200
            assert mock_curate.await_count == 0
            candidates = search_res.json()["results"]
            assert len(candidates) == 1

            pick_res = await client.post(
                "/api/search/ai-pick",
                json={"query": "DuneOnDemandTest", "category": "Movies", "candidates": candidates},
            )
            assert pick_res.status_code == 200
            assert mock_curate.await_count == 1
            body = pick_res.json()
            assert body["ai_ranking"]["recommended_guid"] == "rel-1"
            assert body["ai_ranking"]["ai_active"] is True



