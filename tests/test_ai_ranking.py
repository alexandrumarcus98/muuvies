import pytest
import pytest_asyncio
from backend.app.ai_assistant import (
    parse_release_attributes,
    check_library_duplicate,
    ai_assistant,
)
from backend.app.database import init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_ai_ranking_db():
    await init_db()


def test_parse_release_attributes():
    title = "Dune.Part.Two.2024.2160p.UHD.Remux.DV.HDR10+.TrueHD.Atmos.7.1-Framestor"
    attrs = parse_release_attributes(title)

    assert attrs["resolution"] == "4K 2160p"
    assert attrs["source"] == "REMUX"
    assert attrs["group"] == "framestor"

    labels = [b.label for b in attrs["badges"]]
    assert "4K UHD" in labels
    assert "REMUX" in labels
    assert "Dolby Vision" in labels
    assert "HDR10+" in labels
    assert "Atmos" in labels
    assert "P2P: FRAMESTOR" in labels


def test_check_library_duplicate():
    library_items = [
        {"title": "Interstellar", "year": 2014, "has_file": True},
        {"title": "The Dark Knight", "year": 2008, "has_file": False},
    ]

    matched, note = check_library_duplicate("Interstellar.2014.1080p.BluRay.x264", library_items)
    assert matched is True
    assert "Interstellar" in note

    not_matched, note2 = check_library_duplicate("Oppenheimer.2023.2160p.Remux", library_items)
    assert not_matched is False
    assert note2 is None


@pytest.mark.asyncio
async def test_ai_ranking_heuristic_fallback():
    releases = [
        {
            "guid": "rel-1",
            "title": "Gladiator.II.2024.CAM.XviD",
            "size": 1500000000,
            "size_formatted": "1.40 GB",
            "seeders": 5,
            "leechers": 2,
            "indexer": "PublicTracker",
        },
        {
            "guid": "rel-2",
            "title": "Gladiator.II.2024.2160p.UHD.Remux.DV-Framestor",
            "size": 65000000000,
            "size_formatted": "60.50 GB",
            "seeders": 45,
            "leechers": 10,
            "indexer": "PrivateTracker",
        },
    ]

    enriched, summary = await ai_assistant.analyze_and_rank(
        query="Gladiator 2 best quality",
        releases=releases,
        library_items=[],
    )

    assert len(enriched) == 2
    assert summary is not None
    # Remux Framestor should be picked as top recommendation
    assert summary.recommended_guid == "rel-2"
    rel_2 = next(r for r in enriched if r["guid"] == "rel-2")
    assert rel_2["ai_pick"] is True
    assert rel_2["quality_score"] > 80


@pytest.mark.asyncio
async def test_gemini_503_cascade_to_azure_and_curator_http_200(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from google.genai import errors as genai_errors
    from httpx import ASGITransport, AsyncClient
    from backend.app.config import settings
    from backend.app.main import app
    from backend.app.services.cache import curator_cache, search_cache

    curator_cache.clear()
    search_cache.clear()
    ai_assistant._gemini_cooldown_until = 0.0

    err_503 = genai_errors.ServerError(
        503,
        {"error": {"code": 503, "message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}},
    )
    mock_generate = AsyncMock(side_effect=err_503)
    fake_gemini_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=mock_generate))
    )

    monkeypatch.setattr(settings, "AI_PROVIDER", "auto")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "AZURE_OPENAI_API_KEY", "test-azure-key")
    monkeypatch.setattr(settings, "AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setattr(ai_assistant, "_get_gemini_client", lambda: fake_gemini_client)

    azure_payload = {
        "query_intent": "4K Dune Remux",
        "recommended_title": "Dune.Part.Two.2024.2160p.UHD.Remux-Framestor",
        "recommendation_reason": "Picked via Azure fallback cleanly.",
        "ratings": [{"title": "Dune.Part.Two.2024.2160p.UHD.Remux-Framestor", "score": 97, "note": "Top pick"}],
    }
    mock_azure = AsyncMock(return_value=(azure_payload, "Azure OpenAI (gpt-5-mini)"))
    monkeypatch.setattr(ai_assistant, "_call_azure_openai", mock_azure)

    candidate_releases = [
        {
            "guid": "rel-dune-1",
            "title": "Dune.Part.Two.2024.2160p.UHD.Remux-Framestor",
            "category": "Movies",
            "indexer": "MockTracker",
            "size": 65 * 1024**3,
            "size_formatted": "65.00 GB",
            "seeders": 90,
            "leechers": 5,
        }
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        res = await client.post(
            "/api/search/curator",
            json={"query": "Dune 503 test", "category": "Movies", "candidates": candidate_releases},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ai_ranking"]["ai_active"] is True
        assert body["ai_ranking"]["model_used"] == "Azure OpenAI (gpt-5-mini)"
        assert body["ai_ranking"]["recommended_guid"] == "rel-dune-1"
        assert mock_generate.await_count == 1  # fast-failed immediately on 503 without retrying remaining Gemini models
        assert mock_azure.await_count == 1

        # Second uncached query during cooldown skips Gemini in 0ms and hits Azure directly
        res2 = await client.post(
            "/api/search/curator",
            json={"query": "Dune cooldown test", "category": "Movies", "candidates": candidate_releases},
        )
        assert res2.status_code == 200
        assert mock_generate.await_count == 1
        assert mock_azure.await_count == 2

        # If all LLM providers also fail, /api/search/curator still returns HTTP 200 with heuristic fallback
        curator_cache.clear()
        monkeypatch.setattr(ai_assistant, "_call_azure_openai", AsyncMock(return_value=None))
        monkeypatch.setattr(settings, "GITHUB_TOKEN", "")
        res3 = await client.post(
            "/api/search/curator",
            json={"query": "Dune heuristic fallback", "category": "Movies", "candidates": candidate_releases},
        )
        assert res3.status_code == 200
        body3 = res3.json()
        assert body3["ai_ranking"]["ai_active"] is False
        assert body3["ai_ranking"]["model_used"] == "Heuristic Rules Engine"
        assert body3["ai_ranking"]["recommended_guid"] == "rel-dune-1"


