from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app


@pytest_asyncio.fixture(autouse=True)
async def setup_grab_db():
    await init_db()


@pytest.mark.asyncio
async def test_quick_grab_retries_once_and_returns_422_on_dead_torrent(monkeypatch):
    fake_client = MagicMock()
    monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: fake_client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        # 1. Transient ReadTimeout on attempt 1 -> succeeds on retry attempt 2 with 18s/4s timeout
        bencoded = b"d8:announce3:url4:infod4:name4:teste4:teste"
        fake_client.get = AsyncMock(
            side_effect=[
                httpx.ReadTimeout("timeout 1"),
                MagicMock(status_code=200, content=bencoded, headers={}),
            ]
        )
        fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text="Ok."))

        ok_res = await client.post(
            "/api/grab/quick",
            json={
                "title": "Dune Part Two 2160p",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/1/download?apikey=x",
                "category": "Movies",
                "indexer": "MockTracker",
                "size": 1000,
            },
        )
        assert ok_res.status_code == 200
        assert ok_res.json()["success"] is True
        assert fake_client.get.await_count == 2
        t = fake_client.get.call_args.kwargs["timeout"]
        assert isinstance(t, httpx.Timeout) and t.read == 18.0 and t.connect == 4.0

        # 2. Both attempts fail with no magnet/infoHash -> HTTP 422 Unprocessable Entity (not 502)
        fake_client.get = AsyncMock(side_effect=[httpx.ReadTimeout("t1"), httpx.ReadTimeout("t2")])
        fake_client.post = AsyncMock()
        dead_res = await client.post(
            "/api/grab/quick",
            json={
                "title": "Dead Tracker Release",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/99/download?apikey=x",
                "category": "Movies",
                "indexer": "DeadIndexer",
                "size": 1000,
            },
        )
        assert dead_res.status_code == 422
        assert "Prowlarr .torrent download failed" in dead_res.json()["detail"]
        assert fake_client.get.await_count == 2
        fake_client.post.assert_not_awaited()


def test_clean_release_title_and_lookup_candidates():
    from backend.app.services.arr_client import _clean_release_title, _build_lookup_candidates

    clean_tv, yr_tv = _clean_release_title(
        "Dexter.Resurrection.S02E01.2160p.ATVP.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM"
    )
    assert clean_tv == "Dexter Resurrection"
    assert yr_tv is None

    clean_br, yr_br = _clean_release_title("Blade.Runner.2049.2017.2160p.BluRay.x265-TERMiNAL")
    assert clean_br == "Blade Runner 2049 2017"
    assert yr_br == 2017

    cands_br, _ = _build_lookup_candidates("Blade.Runner.2049.2017.2160p.BluRay.x265-TERMiNAL", is_sonarr=False)
    assert cands_br[:2] == ["Blade Runner 2049 2017", "Blade Runner 2049"]

    clean_sp, yr_sp = _clean_release_title("Spider-Man.No.Way.Home.2021.1080p.WEB-DL.DDP5.1.H264-CMRG")
    assert clean_sp == "Spider-Man No Way Home 2021"
    assert yr_sp == 2021


@pytest.mark.asyncio
async def test_arr_monitor_cleans_release_title_for_sonarr_and_radarr(monkeypatch):
    from backend.app.routers import grab as grab_router

    monkeypatch.setattr(grab_router.sonarr_client, "api_key", "test-sonarr-key")
    monkeypatch.setattr(grab_router.radarr_client, "api_key", "test-radarr-key")

    fake_client = MagicMock()
    monkeypatch.setattr("backend.app.services.arr_client.get_search_client", lambda: fake_client)

    raw_tv = "Dexter.Resurrection.S02E01.2160p.ATVP.WEB-DL.DV.HDR10+.DDP5.1.Atmos.H265.MP4-BTM"
    raw_movie = "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR10+.TrueHD.Atmos-FraMeSToR"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        # 1. Sonarr: cleans release name for /series/lookup, keeps raw release title for /release/push
        fake_client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=lambda: [
                    {
                        "title": "Dexter: Resurrection",
                        "tvdbId": 456789,
                        "titleSlug": "dexter-resurrection",
                        "images": [],
                        "seasons": [],
                        "id": 0,
                    }
                ],
            )
        )
        fake_client.post = AsyncMock(return_value=MagicMock(status_code=201, text="{}"))

        tv_res = await client.post(
            "/api/grab/arr",
            json={
                "title": raw_tv,
                "category": "TV Shows",
                "indexer": "MockTV",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/10/download?apikey=x",
                "size": 5000,
            },
        )
        assert tv_res.status_code == 200
        assert tv_res.json()["target"] == "sonarr"
        assert fake_client.get.call_args.kwargs["params"] == {"term": "Dexter Resurrection"}
        # First POST adds the series, second POST pushes the raw release title
        assert fake_client.post.call_args_list[0].args[0].endswith("/api/v3/series")
        assert fake_client.post.call_args_list[0].kwargs["json"]["title"] == "Dexter: Resurrection"
        assert fake_client.post.call_args_list[1].args[0].endswith("/api/v3/release/push")
        assert fake_client.post.call_args_list[1].kwargs["json"]["title"] == raw_tv

        # 2. Radarr: if "Dune Part Two 2024" returns empty, falls back to "Dune Part Two" and matches year=2024
        fake_client.get = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=lambda: []),
                MagicMock(
                    status_code=200,
                    json=lambda: [
                        {"title": "Dune: Part Two (Fan Edit)", "year": 2023, "tmdbId": 1, "titleSlug": "fan", "id": 0},
                        {"title": "Dune: Part Two", "year": 2024, "tmdbId": 693134, "titleSlug": "dune-part-two-693134", "id": 0},
                    ],
                ),
            ]
        )
        fake_client.post = AsyncMock(return_value=MagicMock(status_code=201, text="{}"))

        mv_res = await client.post(
            "/api/grab/arr",
            json={
                "title": raw_movie,
                "category": "Movies",
                "indexer": "MockMovies",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/20/download?apikey=x",
                "size": 8000,
            },
        )
        assert mv_res.status_code == 200
        assert mv_res.json()["target"] == "radarr"
        assert fake_client.get.call_args_list[0].kwargs["params"] == {"term": "Dune Part Two 2024"}
        assert fake_client.get.call_args_list[1].kwargs["params"] == {"term": "Dune Part Two"}
        assert fake_client.post.call_args_list[0].kwargs["json"]["tmdbId"] == 693134


