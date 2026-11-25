"""Tests for public TMDB trending showcase and same-origin backdrop proxy."""

from __future__ import annotations

import httpx
import pytest

from backend.app.config import settings
from backend.app.main import app
from backend.app.routers import public_media


@pytest.mark.asyncio
async def test_public_trending_empty_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "TMDB_READ_ONLY_API_KEY", "")
    public_media._trending_cache = (0.0, [])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        res = await client.get("/api/public/trending")

    assert res.status_code == 200
    assert res.json() == {"items": []}


@pytest.mark.asyncio
async def test_public_trending_and_backdrop_proxy_mocked(monkeypatch):
    monkeypatch.setattr(settings, "TMDB_READ_ONLY_API_KEY", "test.jwt.token")
    public_media._trending_cache = (0.0, [])
    public_media._backdrop_cache.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/trending/movie/week"):
            assert request.headers.get("Authorization") == "Bearer test.jwt.token"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 693134,
                            "title": "Dune: Part Two",
                            "release_date": "2024-02-27",
                            "overview": "Paul Atreides unites with Chani.",
                            "vote_average": 8.2,
                            "backdrop_path": "/xOMo8BRK7PfcJv9JCnx7s5hj0PX.jpg",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/movie/693134/credits"):
            return httpx.Response(
                200,
                json={"crew": [{"job": "Director", "name": "Denis Villeneuve"}]},
            )
        if request.url.path == "/t/p/w1280/xOMo8BRK7PfcJv9JCnx7s5hj0PX.jpg":
            return httpx.Response(
                200,
                content=b"\xff\xd8\xff\xe0fakejpeg",
                headers={"content-type": "image/jpeg"},
            )
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(public_media, "get_search_client", lambda: mock_client)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            res = await client.get("/api/public/trending")
            assert res.status_code == 200
            items = res.json()["items"]
            assert len(items) == 1
            assert items[0]["title"] == "Dune: Part Two"
            assert items[0]["director"] == "Denis Villeneuve"
            assert items[0]["year"] == "2024"
            assert items[0]["imageSrc"] == "/api/public/backdrop/xOMo8BRK7PfcJv9JCnx7s5hj0PX.jpg"

            img_res = await client.get(items[0]["imageSrc"])
            assert img_res.status_code == 200
            assert img_res.headers["content-type"] == "image/jpeg"
            assert img_res.content == b"\xff\xd8\xff\xe0fakejpeg"

            # Path traversal / non-jpg rejected with 404
            bad_res = await client.get("/api/public/backdrop/not-an-image.png")
            assert bad_res.status_code == 404
    finally:
        await mock_client.aclose()

