"""Tests for Adult Vault pre-download previews: parsers, SSRF, tokens, cache, headers, cross-seed."""

from __future__ import annotations

import json
import logging
import socket
import time
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app
from backend.app.observability import scrub_sentry_event
from backend.app.routers import grab as grab_router
from backend.app.services import adult_preview as ap_svc
from backend.app.services.cache import _PROWLARR_CACHE, search_cache
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.streaming import create_preview_token, create_stream_token, verify_preview_token, verify_stream_token

_TEST_PREVIEW_SOURCES = json.dumps({
    "ForumVault": {
        "hosts": ["forum-vault.example", "www.forum-vault.example"],
        "parser": "bbcode",
    },
    "GalleryVault": {
        "hosts": ["gallery-vault.example", "www.gallery-vault.example"],
        "parser": "gallery",
        "cover_hint": "cdn-cover.example/p/",
        "shot_hint": "cdn-shots.example/1s/",
        "poster_fallback": True,
    },
})
_TEST_IMAGE_HOSTS = "cdn-cover.example,cdn-shots.example"
_TEST_IMAGE_SUBDOMAIN_HOSTS = "img-host.example"
_TEST_ADULT_KEYWORDS = "forumvault,galleryvault,forum-vault.example,gallery-vault.example"


def _make_image_bytes(kind: str, total_size: int = 5120) -> bytes:
    if kind == "jpeg":
        hdr = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    elif kind == "png":
        hdr = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    elif kind == "webp":
        hdr = b"RIFF\x00\x14\x00\x00WEBPVP8 "
    else:
        raise ValueError(kind)
    return hdr + (b"\x00" * max(0, total_size - len(hdr)))


@pytest_asyncio.fixture(autouse=True)
async def _reset_preview_state(monkeypatch):
    await init_db()
    ap_svc.clear_preview_caches()
    search_cache._store.clear() if hasattr(search_cache, "_store") else None
    _PROWLARR_CACHE.clear() if hasattr(_PROWLARR_CACHE, "clear") else None
    monkeypatch.setattr(settings, "ADULT_PREVIEW_SOURCES_JSON", _TEST_PREVIEW_SOURCES)
    monkeypatch.setattr(settings, "ADULT_PREVIEW_IMAGE_HOSTS", _TEST_IMAGE_HOSTS)
    monkeypatch.setattr(settings, "ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS", _TEST_IMAGE_SUBDOMAIN_HOSTS)
    monkeypatch.setattr(settings, "ADULT_INDEXER_KEYWORDS", _TEST_ADULT_KEYWORDS)
    yield
    ap_svc.clear_preview_caches()


def test_bbcode_and_gallery_html_parsers_and_fail_closed_defaults(monkeypatch):
    pl_html = """
    <html><body>
      <div class="post_body">
        <img src="/forum/images/smiles/icon_mrgreen.gif" />
        <var class="postImg" title="https://forum-vault.example/forum/images/logo.png">&#10;</var>
        <var class="postImg" title="https://evil-tracker.example/beacon.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/big/2026/01/cover001.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/thumb/2026/01/shot1.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/thumb/2026/01/shot2.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/thumb/2026/01/shot3.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/thumb/2026/01/shot4.jpg">&#10;</var>
        <var class="postImg" title="https://i99.img-host.example/thumb/2026/01/shot5.jpg">&#10;</var>
      </div>
      <div class="post_body">
        <var class="postImg" title="https://i99.img-host.example/big/2026/01/reply_sig.jpg">&#10;</var>
      </div>
    </body></html>
    """
    pl_imgs = ap_svc.parse_bbcode_preview_images(pl_html, "https://forum-vault.example/forum/viewtopic.php?t=1001")
    assert len(pl_imgs) == 5
    assert pl_imgs[0] == {"url": "https://i99.img-host.example/big/2026/01/cover001.jpg", "role": "cover"}
    assert [x["role"] for x in pl_imgs[1:]] == ["shot", "shot", "shot", "shot"]
    assert all("reply_sig" not in x["url"] and "evil-tracker" not in x["url"] for x in pl_imgs)

    xc_html = """
    <html><body>
      <img src="https://cdn-cover.example/p/c01.jpg" />
      <img src="https://unknown-banner.example/ad.jpg" />
      <img src="https://cdn-shots.example/1s/s1.jpg" />
      <img src="https://cdn-shots.example/1s/s2.jpg" />
      <img data-src="https://cdn-shots.example/1s/s3.jpg" src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==" />
      <img src="https://cdn-shots.example/1s/s4.jpg" />
      <img src="https://cdn-shots.example/1s/s5.jpg" />
    </body></html>
    """
    xc_imgs = ap_svc.parse_gallery_preview_images(
        xc_html,
        "https://gallery-vault.example/torrents/details/1001",
        poster_url="https://cdn-cover.example/p/fallback.jpg",
        cover_hint="cdn-cover.example/p/",
        shot_hint="cdn-shots.example/1s/",
    )
    assert len(xc_imgs) == 5
    assert xc_imgs[0] == {"url": "https://cdn-cover.example/p/c01.jpg", "role": "cover"}
    assert all(x["role"] == "shot" and "cdn-shots.example/1s/" in x["url"] for x in xc_imgs[1:])

    xc_fallback = ap_svc.parse_gallery_preview_images(
        "<html><body><img src='https://cdn-shots.example/1s/s1.jpg' /></body></html>",
        "https://gallery-vault.example/torrents/details/1001",
        poster_url="https://cdn-cover.example/p/fallback.jpg",
        cover_hint="cdn-cover.example/p/",
        shot_hint="cdn-shots.example/1s/",
    )
    assert xc_fallback[0] == {"url": "https://cdn-cover.example/p/fallback.jpg", "role": "cover"}
    assert xc_fallback[1] == {"url": "https://cdn-shots.example/1s/s1.jpg", "role": "shot"}

    # Unconfigured env fails closed cleanly
    monkeypatch.setattr(settings, "ADULT_PREVIEW_SOURCES_JSON", "")
    monkeypatch.setattr(settings, "ADULT_PREVIEW_IMAGE_HOSTS", "")
    monkeypatch.setattr(settings, "ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS", "")
    assert ap_svc.canonical_preview_indexer("ForumVault") is None
    assert ap_svc.is_allowed_page_url("https://forum-vault.example/forum/viewtopic.php?t=1", "ForumVault") is False
    assert ap_svc.is_allowed_image_url("https://cdn-cover.example/p/c01.jpg") is False
    assert ap_svc.get_preview_privacy_hosts() == ()


@pytest.mark.asyncio
async def test_preview_routes_unauthenticated_stealth_404_and_cors_preflight():
    origin = f"https://media.{settings.DOMAIN}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/vault/x/preview/any-token")).status_code == 404
        assert (await client.get("/api/vault/x/preview/img/any-token")).status_code == 404

        pre = await client.options(
            "/api/vault/x/preview/any-token",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert pre.status_code == 200
        assert pre.headers.get("access-control-allow-origin") == origin


@pytest.mark.asyncio
async def test_search_preview_token_not_cached_and_bounded_by_vault_exp(monkeypatch):
    raw_items = [
        {
            "guid": "pl-1",
            "title": "Synthetic.Studio.Release.2026.01.1080p",
            "categories": [{"id": 6000, "name": "XXX"}],
            "indexer": "ForumVault",
            "size": 2_000_000_000,
            "seeders": 42,
            "leechers": 3,
            "downloadUrl": "https://forum-vault.example/forum/dl.php?t=1001",
            "infoUrl": "https://forum-vault.example/forum/viewtopic.php?t=1001",
            "infoHash": "a" * 40,
        },
        {
            "guid": "xc-1",
            "title": "Synthetic.Club.Release.2026.02.1080p",
            "categories": [{"id": 6000, "name": "Adult"}],
            "indexer": "GalleryVault",
            "size": 1_800_000_000,
            "seeders": 30,
            "leechers": 2,
            "downloadUrl": "https://gallery-vault.example/torrents/download/2002",
            "infoUrl": "https://gallery-vault.example/torrents/details/2002",
            "posterUrl": "https://cdn-cover.example/p/c2002.jpg",
            "infoHash": "b" * 40,
        },
        {
            "guid": "tpb-1",
            "title": "Synthetic.Other.Release.2026.03.1080p",
            "categories": [{"id": 6000, "name": "XXX"}],
            "indexer": "PublicVaultFeed",
            "size": 1_500_000_000,
            "seeders": 15,
            "leechers": 1,
            "magnetUrl": "magnet:?xt=urn:btih:" + ("c" * 40),
            "infoUrl": "https://public-feed.example/description.php?id=3",
            "infoHash": "c" * 40,
        },
    ]

    monkeypatch.setattr(prowlarr_client, "_get_enabled_indexer_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(prowlarr_client, "_fetch_search_items", AsyncMock(return_value=raw_items))
    monkeypatch.setattr("backend.app.routers.search.query_library_fts", AsyncMock(return_value=[]))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(
                "/api/auth/login",
                json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
            )
        ).status_code == 200

        # Vault locked -> 404 on Category=Adult, no adult rows leaked in All
        locked_all = await client.post("/api/search", json={"query": "Synthetic", "category": "All"})
        assert locked_all.status_code == 200
        assert locked_all.json()["results"] == []

        # Unlock vault
        unlock = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock.status_code == 200
        unlocked_cookie = client.cookies.get(settings.COOKIE_NAME)
        from backend.app.security import decode_session_token

        session_payload = decode_session_token(unlocked_cookie)
        assert session_payload is not None
        vault_exp = int(session_payload["vault_exp"])

        res = await client.post("/api/search", json={"query": "Synthetic", "category": "Adult"})
        assert res.status_code == 200
        body = res.json()
        assert len(body["results"]) == 3

        by_idx = {r["indexer"]: r for r in body["results"]}
        assert by_idx["ForumVault"]["preview_token"]
        assert by_idx["GalleryVault"]["preview_token"]
        assert by_idx["PublicVaultFeed"]["preview_token"] is None

        # Raw URLs must never appear in serialized API response
        for r in body["results"]:
            assert "info_url" not in r
            assert "poster_url" not in r
            assert "infoUrl" not in r
            assert "posterUrl" not in r

        # Preview token exp must not outlive session vault_exp
        pl_tok_data = verify_preview_token(by_idx["ForumVault"]["preview_token"], expected_kind="adult-preview")
        assert pl_tok_data is not None
        assert int(pl_tok_data["exp"]) <= vault_exp

        # Shared 60s search_cache entry dicts must NOT contain minted preview_token
        for _k, entry in list(getattr(search_cache, "_entries", {}).items()):
            items_cached = entry.value[0] if hasattr(entry, "value") else []
            for cached_dict in items_cached:
                assert "preview_token" not in cached_dict


@pytest.mark.asyncio
async def test_preview_client_refuses_cookies_omits_referer_and_disallows_prowlarr(monkeypatch):
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            headers={"Set-Cookie": "secret_tracker_sid=abc123; Path=/; HttpOnly"},
            text="<html><body><var class='postImg' title='https://i1.img-host.example/big/2026/a.jpg'></var></body></html>",
        )

    from backend.app.services import http_clients as hc

    mocked_client = hc._build_preview_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ap_svc, "get_preview_client", lambda: mocked_client)

    called_kwargs: list[dict] = []

    async def fake_validate(url: str, *, allow_prowlarr: bool = True):
        called_kwargs.append({"url": url, "allow_prowlarr": allow_prowlarr})
        return True, ""

    monkeypatch.setattr(ap_svc, "validate_download_url", fake_validate)

    try:
        res1 = await ap_svc.resolve_preview_images(
            indexer="ForumVault",
            info_url="https://forum-vault.example/forum/viewtopic.php?t=1",
            session_key="alice",
        )
        assert len(res1) == 1
        ap_svc._list_cache.clear()
        await ap_svc.resolve_preview_images(
            indexer="ForumVault",
            info_url="https://forum-vault.example/forum/viewtopic.php?t=2",
            session_key="alice",
        )
    finally:
        await mocked_client.aclose()

    assert len(seen_headers) == 2
    for hdrs in seen_headers:
        low_keys = {k.lower() for k in hdrs}
        assert "cookie" not in low_keys
        assert "referer" not in low_keys

    assert called_kwargs and all(c["allow_prowlarr"] is False for c in called_kwargs)


def test_sentry_scrubs_preview_host_breadcrumbs():
    event = {
        "request": {"url": "https://media.example.com/api/search", "data": {"query": "Dune", "category": "Movies"}},
        "breadcrumbs": {
            "values": [
                {"category": "httplib", "data": {"url": "https://forum-vault.example/forum/viewtopic.php?t=5", "method": "GET"}},
                {"category": "httplib", "data": {"url": "https://gallery-vault.example/torrents/details/7"}},
                {"category": "httplib", "data": {"url": "https://i99.img-host.example/big/2026/a.jpg"}},
                {"category": "httplib", "data": {"url": "https://cdn-cover.example/p/b.jpg"}},
                {"category": "httplib", "data": {"url": "https://cdn-shots.example/1s/c.jpg"}},
                {"category": "httplib", "data": {"url": "https://api.themoviedb.org/3/movie/1"}},
            ]
        },
    }
    scrubbed = scrub_sentry_event(event)
    assert scrubbed is not None
    urls = [c["data"]["url"] for c in scrubbed["breadcrumbs"]["values"]]
    assert urls == ["https://api.themoviedb.org/3/movie/1"]


@pytest.mark.asyncio
async def test_token_kind_separation_vault_404_ssrf_and_image_validation(monkeypatch):
    valid_jpeg = _make_image_bytes("jpeg", 5120)
    valid_png = _make_image_bytes("png", 5120)
    valid_webp = _make_image_bytes("webp", 5120)

    def _dns_public(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

    def _dns_private(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 443))]

    monkeypatch.setattr("backend.app.services.url_safety.socket.getaddrinfo", _dns_public)

    from backend.app.services import http_clients as hc

    responses = {}

    def handler(request: httpx.Request) -> httpx.Response:
        entry = responses.get(str(request.url))
        if callable(entry):
            return entry()
        return entry or httpx.Response(404)

    mocked_client = hc._build_preview_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ap_svc, "get_preview_client", lambda: mocked_client)

    page_url = "https://forum-vault.example/forum/viewtopic.php?t=777"
    responses[page_url] = httpx.Response(
        200,
        text=(
            "<div class='post_body'>"
            "<var class='postImg' title='https://i1.img-host.example/big/2026/j.jpg'></var>"
            "<var class='postImg' title='https://i1.img-host.example/thumb/2026/p.png'></var>"
            "<var class='postImg' title='https://i1.img-host.example/thumb/2026/w.webp'></var>"
            "<var class='postImg' title='https://i1.img-host.example/thumb/2026/bad.html'></var>"
            "<var class='postImg' title='https://i1.img-host.example/thumb/2026/tiny.jpg'></var>"
            "</div>"
        ),
    )
    responses["https://i1.img-host.example/big/2026/j.jpg"] = httpx.Response(
        200,
        headers={"Content-Type": "text/plain"},
        content=valid_jpeg,
    )
    responses["https://i1.img-host.example/thumb/2026/p.png"] = httpx.Response(200, content=valid_png)
    responses["https://i1.img-host.example/thumb/2026/w.webp"] = httpx.Response(200, content=valid_webp)
    responses["https://i1.img-host.example/thumb/2026/bad.html"] = httpx.Response(
        200, content=(b"<html>not an image</html>" * 300)
    )
    responses["https://i1.img-host.example/thumb/2026/tiny.jpg"] = httpx.Response(
        200, content=_make_image_bytes("jpeg", 512)
    )

    # Token separation unit checks
    prev_tok = create_preview_token(
        {"indexer": "ForumVault", "info_url": page_url, "sub": settings.ADMIN_USERNAME},
        vault_exp=int(time.time()) + 600,
    )
    stream_tok = create_stream_token({"item_id": "radarr:1", "title": "Movie", "is_adult": False})
    assert verify_stream_token(prev_tok) is None
    assert verify_preview_token(stream_tok) is None

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (
                await client.post(
                    "/api/auth/login",
                    json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
                )
            ).status_code == 200

            # Locked vault -> 404 on both preview routes
            assert (await client.get(f"/api/vault/x/preview/{prev_tok}")).status_code == 404
            assert (await client.get(f"/api/vault/x/preview/img/{prev_tok}")).status_code == 404

            # Cross-use: preview token rejected on stream route
            assert (await client.get(f"/api/stream/file/{prev_tok}")).status_code == 401

            # Unlock vault
            assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 200

            # Stream token rejected on preview route, tampered/expired/wrong-sub rejected -> 404
            assert (await client.get(f"/api/vault/x/preview/{stream_tok}")).status_code == 404
            assert (await client.get(f"/api/vault/x/preview/{prev_tok}tampered")).status_code == 404
            assert (await client.get(f"/api/vault/x/preview/img/{prev_tok}")).status_code == 404

            expired_tok = create_preview_token(
                {"indexer": "ForumVault", "info_url": page_url, "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) - 10,
            )
            assert (await client.get(f"/api/vault/x/preview/{expired_tok}")).status_code == 404

            other_sub_tok = create_preview_token(
                {"indexer": "ForumVault", "info_url": page_url, "sub": "someone_else"},
                vault_exp=int(time.time()) + 600,
            )
            assert (await client.get(f"/api/vault/x/preview/{other_sub_tok}")).status_code == 404

            # Valid preview fetch
            res = await client.get(f"/api/vault/x/preview/{prev_tok}")
            assert res.status_code == 200
            assert res.headers.get("cache-control") == "no-store, private"
            assert res.headers.get("x-content-type-options") == "nosniff"
            data = res.json()
            assert data["source"] == "ForumVault"
            assert len(data["images"]) == 5

            # JPEG with lied upstream Content-Type -> magic-sniffed image/jpeg + privacy headers
            img0 = await client.get(f"/api/vault/x/preview/img/{data['images'][0]['id']}")
            assert img0.status_code == 200
            assert img0.headers.get("content-type", "").startswith("image/jpeg")
            assert img0.headers.get("cache-control") == "no-store, private"
            assert img0.headers.get("x-content-type-options") == "nosniff"
            assert img0.headers.get("referrer-policy") == "no-referrer"
            assert img0.headers.get("content-disposition") == "inline"

            img1 = await client.get(f"/api/vault/x/preview/img/{data['images'][1]['id']}")
            assert img1.status_code == 200 and img1.headers.get("content-type", "").startswith("image/png")

            img2 = await client.get(f"/api/vault/x/preview/img/{data['images'][2]['id']}")
            assert img2.status_code == 200 and img2.headers.get("content-type", "").startswith("image/webp")

            # Non-image bytes -> 415; < 4 KB image -> 422
            assert (await client.get(f"/api/vault/x/preview/img/{data['images'][3]['id']}")).status_code == 415
            assert (await client.get(f"/api/vault/x/preview/img/{data['images'][4]['id']}")).status_code == 422

            # Oversized > 2 MB -> 413 (streamed with NO Content-Length header)
            huge_url = "https://i1.img-host.example/big/2026/huge.jpg"
            responses[huge_url] = httpx.Response(
                200,
                content=b"\xff\xd8\xff\xe0" + (b"x" * ((2 * 1024 * 1024) + 128)),
            )
            huge_tok = create_preview_token(
                {"indexer": "ForumVault", "url": huge_url, "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) + 600,
                kind="adult-preview-img",
            )
            assert (await client.get(f"/api/vault/x/preview/img/{huge_tok}")).status_code == 413

            # Off-allowlist host -> 404
            off_page = create_preview_token(
                {"indexer": "ForumVault", "info_url": "https://evil.example/topic", "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) + 600,
            )
            assert (await client.get(f"/api/vault/x/preview/{off_page}")).status_code == 404

            off_img = create_preview_token(
                {"indexer": "ForumVault", "url": "https://evil.example/pic.jpg", "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) + 600,
                kind="adult-preview-img",
            )
            assert (await client.get(f"/api/vault/x/preview/img/{off_img}")).status_code == 404

            # Redirect to loopback 127.0.0.1 -> blocked (404)
            redir_page = "https://forum-vault.example/forum/viewtopic.php?t=888"
            responses[redir_page] = httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
            redir_tok = create_preview_token(
                {"indexer": "ForumVault", "info_url": redir_page, "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) + 600,
            )
            assert (await client.get(f"/api/vault/x/preview/{redir_tok}")).status_code == 404

            # Private DNS resolution rejected (404)
            monkeypatch.setattr("backend.app.services.url_safety.socket.getaddrinfo", _dns_private)
            dns_page = "https://forum-vault.example/forum/viewtopic.php?t=999"
            responses[dns_page] = httpx.Response(200, text="<html></html>")
            dns_tok = create_preview_token(
                {"indexer": "ForumVault", "info_url": dns_page, "sub": settings.ADMIN_USERNAME},
                vault_exp=int(time.time()) + 600,
            )
            assert (await client.get(f"/api/vault/x/preview/{dns_tok}")).status_code == 404
    finally:
        await mocked_client.aclose()


@pytest.mark.asyncio
async def test_preview_rate_limit_429_and_no_sensitive_logs(monkeypatch, caplog):
    from backend.app.services import http_clients as hc

    monkeypatch.setattr(
        "backend.app.services.url_safety.socket.getaddrinfo",
        lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<div class='post_body'><var class='postImg' title='https://i1.img-host.example/big/2026/a.jpg'></var></div>",
        )

    mocked_client = hc._build_preview_client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ap_svc, "get_preview_client", lambda: mocked_client)

    vault_exp = int(time.time()) + 600
    tokens = [
        create_preview_token(
            {
                "indexer": "ForumVault",
                "info_url": f"https://forum-vault.example/forum/viewtopic.php?t=secret777{i}",
                "sub": settings.ADMIN_USERNAME,
            },
            vault_exp=vault_exp,
        )
        for i in range(31)
    ]

    try:
        with caplog.at_level(logging.INFO, logger="muuvies.adult_preview"):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                assert (
                    await client.post(
                        "/api/auth/login",
                        json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
                    )
                ).status_code == 200
                assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 200

                # Re-opening an already-cached preview must not consume the rate-limit budget
                for _ in range(3):
                    assert (await client.get(f"/api/vault/x/preview/{tokens[0]}")).status_code == 200

                for tok in tokens[1:30]:
                    assert (await client.get(f"/api/vault/x/preview/{tok}")).status_code == 200
                assert (await client.get(f"/api/vault/x/preview/{tokens[30]}")).status_code == 429
    finally:
        await mocked_client.aclose()

    joined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "Adult preview resolved" in joined_logs
    assert "forum-vault.example" not in joined_logs
    assert "secret777" not in joined_logs
    assert "img-host.example" not in joined_logs


@pytest.mark.asyncio
async def test_adult_grab_skips_cross_seed_and_blocks_whisparr_route(monkeypatch):
    monkeypatch.setattr(
        grab_router.qb_client,
        "add_torrent_with_status",
        AsyncMock(return_value=(True, None, 200)),
    )
    cs_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(grab_router.cross_seed_client, "trigger_search", cs_mock)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(
                "/api/auth/login",
                json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
            )
        ).status_code == 200

        # Locked vault -> /api/grab/arr on adult returns 404 stealth
        locked_arr = await client.post(
            "/api/grab/arr",
            json={
                "title": "Synthetic.Studio.Release.2026.1080p",
                "magnet_url": "magnet:?xt=urn:btih:" + ("d" * 40),
                "category": "Adult",
                "indexer": "ForumVault",
                "size": 1024,
            },
        )
        assert locked_arr.status_code == 404

        assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 200

        # Unlocked vault -> /api/grab/arr on adult is rejected (400; qBittorrent-only)
        adult_arr = await client.post(
            "/api/grab/arr",
            json={
                "title": "Synthetic.Studio.Release.2026.1080p",
                "magnet_url": "magnet:?xt=urn:btih:" + ("d" * 40),
                "category": "Adult",
                "indexer": "ForumVault",
                "size": 1024,
            },
        )
        assert adult_arr.status_code == 400

        # 1. Adult grab skips cross-seed
        adult_res = await client.post(
            "/api/grab/quick",
            json={
                "title": "Synthetic.Studio.Release.2026.1080p",
                "magnet_url": "magnet:?xt=urn:btih:" + ("d" * 40),
                "info_hash": "d" * 40,
                "category": "Adult",
                "indexer": "ForumVault",
                "size": 1024,
            },
        )
        assert adult_res.status_code == 200
        cs_mock.assert_not_awaited()

        # 2. Normal movie grab still triggers cross-seed
        movie_res = await client.post(
            "/api/grab/quick",
            json={
                "title": "Synthetic.Movie.2026.1080p.WEB-DL",
                "magnet_url": "magnet:?xt=urn:btih:" + ("e" * 40),
                "info_hash": "e" * 40,
                "category": "Movies",
                "indexer": "MockMovies",
                "size": 1024,
            },
        )
        assert movie_res.status_code == 200
        cs_mock.assert_awaited_once_with(info_hash="e" * 40)
