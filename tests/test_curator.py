import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from google.genai import errors as genai_errors
from httpx import ASGITransport, AsyncClient

from backend.app.ai_assistant import ai_assistant
from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app
from backend.app.services.cache import curator_cache, search_cache


@pytest_asyncio.fixture(autouse=True)
async def setup_curator_db():
    await init_db()
    curator_cache.clear()
    search_cache.clear()
    ai_assistant._gemini_cooldown_until = 0.0


@pytest.mark.asyncio
async def test_curator_graceful_heuristic_fallback_on_503_connect_and_gaierror(monkeypatch, caplog):
    err_503 = genai_errors.ServerError(
        503,
        {"error": {"code": 503, "message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}},
    )
    fake_gemini = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=AsyncMock(side_effect=err_503)))
    )

    monkeypatch.setattr(settings, "AI_PROVIDER", "auto")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setattr(settings, "AZURE_OPENAI_API_KEY", "test-azure-key")
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "test-github-token")
    monkeypatch.setattr(ai_assistant, "_get_gemini_client", lambda: fake_gemini)
    monkeypatch.setattr(ai_assistant, "_call_azure_openai", AsyncMock(side_effect=httpx.ConnectError("Azure down")))
    monkeypatch.setattr(ai_assistant, "_call_github_models", AsyncMock(side_effect=socket.gaierror(-2, "Name or service not known")))

    candidates = [
        {
            "guid": "cand-remux-1",
            "title": "Dune.Part.Two.2024.2160p.UHD.Remux.DV-Framestor",
            "category": "Movies",
            "indexer": "PrivateMovies",
            "size": 68 * 1024**3,
            "size_formatted": "68.00 GB",
            "seeders": 120,
            "leechers": 4,
        },
        {
            "guid": "cand-cam-2",
            "title": "Dune.Part.Two.2024.CAM-Unknown",
            "category": "Movies",
            "indexer": "Public",
            "size": 2 * 1024**3,
            "size_formatted": "2.00 GB",
            "seeders": 10,
            "leechers": 5,
        },
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        with caplog.at_level(logging.WARNING):
            res = await client.post(
                "/api/search/curator",
                json={"query": "Dune Part Two", "category": "Movies", "candidates": candidates},
            )
        assert res.status_code == 200
        body = res.json()
        assert body["ai_ranking"]["ai_active"] is False
        assert body["ai_ranking"]["model_used"] == "Heuristic Rules Engine"
        assert body["ai_ranking"]["recommended_guid"] == "cand-remux-1"

        # Even if curate_top_candidates itself hits an unexpected query_library_fts/gaierror exception, route still returns 200
        monkeypatch.setattr("backend.app.routers.search.query_library_fts", AsyncMock(side_effect=socket.gaierror(-2, "DNS failed")))
        res_err = await client.post(
            "/api/search/curator",
            json={"query": "Dune Part Two", "category": "Movies", "candidates": candidates},
        )
        assert res_err.status_code == 200
        assert res_err.json()["ai_ranking"]["model_used"] == "Heuristic Rules Engine"

