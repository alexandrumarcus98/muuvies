"""Unit and endpoint tests for WSM scoring, multi-tracker deduplication, and Deep Search RRF."""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app
from backend.app.routers.search import (
    _PROWLARR_CACHE,
    CACHE_TTL_SECONDS,
    MAX_CACHE_ENTRIES,
    _build_dedup_key,
    _normalize_title,
    _prowlarr_cache_key,
    deduplicate_and_score_results,
    expand_deep_queries,
    fuse_deep_search_lists,
)
from backend.app.database import AsyncSessionLocal
from backend.app.models import GrabHistory
from backend.app.services.cache import search_cache
from backend.app.services.library_mirror import replace_service_library_items


@pytest_asyncio.fixture(autouse=True)
async def _init_scoring_test_db(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "PRIVATE_TRACKERS", "private-tracker.example")


def _release(
    *,
    guid: str,
    title: str,
    indexer: str,
    seeders: int,
    leechers: int = 2,
    size_gb: float = 8.0,
    info_hash: str | None = None,
    magnet_url: str | None = None,
    download_url: str | None = "https://tracker.local/dl/1",
    indexer_id: int = 1,
    category: str = "Movies",
) -> dict:
    size_bytes = int(size_gb * (1024 ** 3))
    return {
        "guid": guid,
        "title": title,
        "indexer": indexer,
        "indexerId": indexer_id,
        "indexer_id": indexer_id,
        "seeders": seeders,
        "leechers": leechers,
        "size": size_bytes,
        "size_formatted": f"{size_gb:.2f} GB",
        "infoHash": info_hash,
        "info_hash": info_hash,
        "magnetUrl": magnet_url,
        "magnet_url": magnet_url,
        "downloadUrl": download_url,
        "download_url": download_url,
        "category": category,
    }


def test_normalize_title_strips_diacritics_and_noise():
    assert _normalize_title("Amélie (2001) 1080p WEB-DL x264-GRP") == "amelie 2001"
    assert _normalize_title("Café.de.Flore.2011.2160p.HEVC") == "cafe de flore 2011"


def test_build_dedup_key_prefers_info_hash_then_title_size_mb():
    h = "A" * 40
    with_hash = _release(
        guid="g1",
        title="Dune.Part.Two.2024.1080p.WEB-DL",
        indexer="Knaben",
        seeders=50,
        info_hash=h,
    )
    assert _build_dedup_key(with_hash) == h.lower()

    no_hash = _release(
        guid="g2",
        title="Dune.Part.Two.2024.1080p.WEB-DL",
        indexer="The Pirate Bay",
        seeders=40,
        size_gb=8.0,
        info_hash="short",
    )
    expected_mb = round(no_hash["size"] / 1048576)
    assert _build_dedup_key(no_hash) == f"dune part two 2024_{expected_mb}"


def test_deduplicate_dht_max_public_vs_sum_private_and_source_promotion():
    h = "0123456789abcdef0123456789abcdef01234567"
    pub1 = _release(
        guid="pub-1",
        title="Inception.2010.2160p.WEB-DL.AAC.MP4",
        indexer="Knaben",
        indexer_id=9,
        seeders=100,
        leechers=10,
        size_gb=18.0,
        info_hash=h,
        magnet_url="magnet:?xt=urn:btih:" + h,
        download_url=None,
    )
    pub2 = _release(
        guid="pub-2",
        title="Inception.2010.2160p.WEB-DL.AAC.MP4",
        indexer="The Pirate Bay",
        indexer_id=10,
        seeders=140,
        leechers=15,
        size_gb=18.0,
        info_hash=h,
        magnet_url="magnet:?xt=urn:btih:" + h,
        download_url=None,
    )
    priv = _release(
        guid="priv-1",
        title="Inception.2010.2160p.WEB-DL.AAC.MP4",
        indexer="private-tracker.example",
        indexer_id=22,
        seeders=25,
        leechers=1,
        size_gb=18.0,
        info_hash=h,
        download_url="https://private-tracker.example/download/999",
    )
    results = deduplicate_and_score_results("Inception", [pub1, pub2, priv], category="movie")
    assert len(results) == 1
    merged = results[0]
    # Public trackers take max(100, 140) = 140 instead of 240, then add distinct private 25 -> 165
    assert merged["seeders"] == 165
    assert merged["best_source"] == "private-tracker.example"
    assert merged["indexer"] == "private-tracker.example"
    assert merged["indexerId"] == 22
    assert merged["guid"] == "priv-1"
    assert merged["downloadUrl"] == "https://private-tracker.example/download/999"
    assert merged["sources"] == ["Knaben", "The Pirate Bay", "private-tracker.example"]


def test_wsm_4k_private_bonus_and_avi_remux_penalties():
    good_4k_private = _release(
        guid="good-4k",
        title="Interstellar.2014.2160p.WEB-DL.DV.AAC.MP4",
        indexer="private-tracker.example",
        seeders=90,
        leechers=2,
        size_gb=20.0,
        info_hash="b" * 40,
        download_url="https://private-tracker.example/dl/1",
    )
    heavy_remux = _release(
        guid="remux",
        title="Interstellar.2014.2160p.UHD.BluRay.REMUX.TrueHD.DTS-HD",
        indexer="Knaben",
        seeders=90,
        leechers=2,
        size_gb=75.0,
        info_hash="c" * 40,
        download_url="https://knaben.org/dl/2",
    )
    legacy_avi = _release(
        guid="avi",
        title="Interstellar.2014.1080p.BDRip.avi",
        indexer="Knaben",
        seeders=90,
        leechers=2,
        size_gb=2.0,
        info_hash="d" * 40,
        download_url="https://knaben.org/dl/3",
    )
    scored = deduplicate_and_score_results(
        "Interstellar", [legacy_avi, heavy_remux, good_4k_private], category="movie"
    )
    assert [r["guid"] for r in scored] == ["good-4k", "remux", "avi"]
    assert scored[0]["muuvies_score"] > scored[1]["muuvies_score"] > scored[2]["muuvies_score"]


def test_dynamic_gaussian_profiles_and_relevance_fallback():
    movie_20gb = _release(
        guid="m20",
        title="Dune.Part.Two.2024.2160p.WEB-DL.AAC.MP4",
        indexer="Knaben",
        seeders=50,
        size_gb=20.0,
        info_hash="e" * 40,
    )
    movie_3gb = _release(
        guid="m3",
        title="Dune.Part.Two.2024.2160p.WEB-DL.AAC.MP4",
        indexer="Knaben",
        seeders=50,
        size_gb=3.0,
        info_hash="f" * 40,
    )
    movie_ranked = deduplicate_and_score_results("Dune Part Two", [movie_3gb, movie_20gb], category="movie")
    assert movie_ranked[0]["guid"] == "m20"

    adult_4gb = _release(
        guid="a4",
        title="Sumiko.Smile.2160p.MP4",
        indexer="VaultForum",
        seeders=30,
        size_gb=4.0,
        info_hash="1" * 40,
        category="Adult",
    )
    adult_25gb = _release(
        guid="a25",
        title="Sumiko.Smile.2160p.MP4",
        indexer="VaultForum",
        seeders=30,
        size_gb=25.0,
        info_hash="2" * 40,
        category="Adult",
    )
    adult_ranked = deduplicate_and_score_results("sumiko smile 2160p", [adult_25gb, adult_4gb], category="adult")
    assert adult_ranked[0]["guid"] == "a4"

    # Relevance fallback keeps results if strict >= 0.55 token overlap drops everything
    unrelated = _release(
        guid="fallback",
        title="Completely.Different.Alias.2024.1080p.WEB-DL.MP4",
        indexer="Knaben",
        seeders=15,
        size_gb=2.2,
        info_hash="3" * 40,
        category="TV",
    )
    tv_ranked = deduplicate_and_score_results("Severance S02E01", [unrelated], category="tv")
    assert len(tv_ranked) == 1
    assert tv_ranked[0]["guid"] == "fallback"


def test_fuse_deep_search_lists_bridges_infohash_and_title_size_mb():
    h = "fedcba9876543210fedcba9876543210fedcba98"
    list_a = deduplicate_and_score_results(
        "The Matrix",
        [
            _release(
                guid="pub-mat",
                title="The.Matrix.1999.2160p.WEB-DL.AAC.MP4",
                indexer="Knaben",
                seeders=80,
                size_gb=18.0,
                info_hash=h,
                magnet_url="magnet:?xt=urn:btih:" + h,
                download_url=None,
            ),
            _release(
                guid="other-mat",
                title="The.Matrix.1999.1080p.BluRay",
                indexer="The Pirate Bay",
                seeders=25,
                size_gb=8.0,
                info_hash="4" * 40,
            ),
        ],
        category="movie",
    )
    list_b = deduplicate_and_score_results(
        "Matrix 1999 2160p",
        [
            _release(
                guid="priv-mat",
                title="The.Matrix.1999.2160p.WEB-DL.AAC.MP4",
                indexer="private-tracker.example",
                indexer_id=33,
                seeders=40,
                size_gb=18.0,
                info_hash=None,  # Missing infoHash on second tracker, same normalized title + size MB
                download_url="https://private-tracker.example/dl/matrix",
            ),
        ],
        category="movie",
    )
    fused = fuse_deep_search_lists([list_a, list_b], k=60, wsm_weight=0.35)
    assert len(fused) == 2
    top = fused[0]
    assert top["best_source"] == "private-tracker.example"
    assert set(top["sources"]) == {"Knaben", "private-tracker.example"}
    assert top["info_hash"] == h
    assert top["downloadUrl"] == "https://private-tracker.example/dl/matrix"


def test_expand_deep_queries_limit_and_variants():
    queries = expand_deep_queries(
        "The Matrix 4k",
        category="Movies",
        library_items=[{"title": "The Matrix", "year": 1999}],
    )
    assert 1 <= len(queries) <= 3
    assert queries[0] == "The Matrix 4k"
    assert any("1999" in q or "2160p" in q for q in queries[1:])

    alias_queries = expand_deep_queries(
        "Dune Part Two 2024 2160p WEB-DL",
        category="Movies",
        local_aliases=["Arrakis Chronicles: Part Two"],
    )
    assert 2 <= len(alias_queries) <= 3
    assert alias_queries[0] == "Dune Part Two 2024 2160p WEB-DL"
    assert "Arrakis Chronicles Part Two" in alias_queries
    assert "Dune Part Two" in alias_queries


def test_prowlarr_cache_ttl_fifo_and_vault_isolation():
    assert CACHE_TTL_SECONDS == 600
    assert MAX_CACHE_ENTRIES == 250
    _PROWLARR_CACHE.clear()

    k_locked = _prowlarr_cache_key("Sumiko Smile!", "adult", False)
    k_unlocked = _prowlarr_cache_key("sumiko smile", "adult", True)
    assert k_locked[:2] == k_unlocked[:2]
    assert k_locked != k_unlocked

    _PROWLARR_CACHE.set(k_locked, [{"guid": "locked"}])
    _PROWLARR_CACHE.set(k_unlocked, [{"guid": "unlocked"}])
    assert _PROWLARR_CACHE.get(k_locked)[0]["guid"] == "locked"
    assert _PROWLARR_CACHE.get(k_unlocked)[0]["guid"] == "unlocked"

    # FIFO bound at MAX_CACHE_ENTRIES (250)
    _PROWLARR_CACHE.clear()
    for i in range(MAX_CACHE_ENTRIES + 5):
        _PROWLARR_CACHE.set((f"q{i}", "movie", False), [{"guid": f"g{i}"}])
    assert len(_PROWLARR_CACHE._cache) == MAX_CACHE_ENTRIES
    assert _PROWLARR_CACHE.get(("q0", "movie", False)) is None
    assert _PROWLARR_CACHE.get((f"q{MAX_CACHE_ENTRIES + 4}", "movie", False)) is not None
    _PROWLARR_CACHE.clear()


@pytest.mark.asyncio
async def test_api_search_deep_mode_staggers_and_fuses():
    search_cache._cache.clear()
    _PROWLARR_CACHE.clear()
    calls: list[str] = []

    async def fake_prowlarr_search(query: str, category: str = "All", vault_unlocked: bool = False, limit: int = 100):
        calls.append(query)
        h = "9999888877776666555544443333222211110000"
        if "2160p" in query or "4k" in query.lower():
            return [
                _release(
                    guid="deep-priv",
                    title="Arrival.2016.2160p.WEB-DL.AAC.MP4",
                    indexer="private-tracker.example",
                    indexer_id=55,
                    seeders=60,
                    size_gb=19.0,
                    info_hash=h,
                    download_url="https://private-tracker.example/dl/arrival",
                )
            ]
        return [
            _release(
                guid="deep-pub",
                title="Arrival.2016.2160p.WEB-DL.AAC.MP4",
                indexer="Knaben",
                indexer_id=9,
                seeders=120,
                size_gb=19.0,
                info_hash=h,
                magnet_url="magnet:?xt=urn:btih:" + h,
                download_url=None,
            )
        ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        with patch("backend.app.services.prowlarr.prowlarr_client.search", new=AsyncMock(side_effect=fake_prowlarr_search)):
            res = await client.post(
                "/api/search",
                json={"query": "Arrival", "category": "Movies", "ai_smart_search": False, "deep": True},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["deep_active"] is True
            assert len(calls) >= 2
            assert len(data["results"]) == 1
            item = data["results"][0]
            assert item["best_source"] == "private-tracker.example"
            assert set(item["sources"]) == {"Knaben", "private-tracker.example"}
            assert item["muuvies_score"] is not None

            # Re-sorting uses cached Prowlarr lists without firing new upstream calls
            calls_before = len(calls)
            res_sorted = await client.get(
                "/api/search?q=Arrival&category=Movies&deep=true&sort_by=seeders"
            )
            assert res_sorted.status_code == 200
            assert len(calls) == calls_before


@pytest.mark.asyncio
async def test_local_fts5_suggest_and_vault_isolation():
    await replace_service_library_items(
        "radarr",
        [{"id": 9001, "title": "Solaris 1972 2160p", "cleanTitle": "solaris", "year": 1972, "hasFile": True}],
    )
    await replace_service_library_items(
        "whisparr",
        [{"id": 9002, "title": "Solaris XXX Uncut 2160p", "cleanTitle": "solaris xxx", "year": 2024, "hasFile": True}],
    )
    async with AsyncSessionLocal() as db:
        db.add(GrabHistory(title="Solaris History 1080p", category="Movies", target="radarr", info_hash="s1" * 20))
        db.add(GrabHistory(title="Solaris Vault Clip 2160p", category="Adult", target="whisparr", info_hash="s2" * 20))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )

        # Too short -> []
        short_res = await client.get("/api/suggest?q=s")
        assert short_res.status_code == 200
        assert short_res.json() == []

        # Locked vault must never leak whisparr or Adult grab_history
        locked_res = await client.get("/api/suggest?q=sola")
        assert locked_res.status_code == 200
        locked_items = locked_res.json()
        locked_titles = [it["title"] for it in locked_items]
        assert "Solaris 1972 2160p" in locked_titles
        assert "Solaris History 1080p" in locked_titles
        assert not any("XXX" in t or "Vault" in t for t in locked_titles)
        lib_entry = next(it for it in locked_items if it["title"] == "Solaris 1972 2160p")
        assert lib_entry["source"] == "library"
        assert lib_entry["year"] == 1972
        assert lib_entry["res"] == "2160p"

        # Unlock Adult Vault -> whisparr & adult history appear
        pin_res = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert pin_res.status_code == 200
        unlocked_res = await client.get("/api/suggest?q=sola")
        assert unlocked_res.status_code == 200
        unlocked_titles = [it["title"] for it in unlocked_res.json()]
        assert "Solaris XXX Uncut 2160p" in unlocked_titles

