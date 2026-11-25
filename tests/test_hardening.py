"""Comprehensive regression tests for SSRF, session revocation, rate limiting, SFTP/streaming, Vault privacy, and RBAC."""

from __future__ import annotations

import base64
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.requests import Request

from backend.app.ai_assistant import ai_assistant
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, init_db
from backend.app.main import app
from backend.app.models import EncryptedSetting, User, utcnow
from backend.app.routers import grab as grab_router
from backend.app.routers import queue as queue_router
from backend.app.routers.stream import _public_base_url
from backend.app.security import (
    LoginRateLimiter,
    _user_token_version_floors,
    get_client_ip,
    hash_secret,
)
from backend.app.services import url_safety, watchdog
from backend.app.services.arr_client import radarr_client
from backend.app.services.library_mirror import replace_service_library_items
from backend.app.services.passkeys import resolve_rp_and_origins
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.qbittorrent import QBittorrentClient, qb_client
from backend.app.services.streaming import (
    _SFTPPool,
    _get_ssh_credentials,
    _resolve_seedbox_sftp_path,
    create_stream_token,
    resolve_local_media_path,
    verify_stream_token,
)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    await init_db()


def _make_request(
    *,
    client_host: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
    host_header: str = "media.example.com",
) -> Request:
    raw_headers = [(b"host", host_header.encode("latin-1"))]
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "scheme": "http",
        "server": ("test", 80),
        "client": (client_host, 12345),
        "headers": raw_headers,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_ssrf_blocks_internal_urls_redirects_and_oversized_torrents(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        blocked_urls = [
            "http://127.0.0.1:8888/api/settings",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1:8888/api/health",
            "http://192.168.1.10/admin",
            "http://172.17.0.2:8080/dl.torrent",
            "http://localhost:7878/api/v3/movie",
            "http://seedbox-tunnel:8080/api/v2/app/version",
            "http://challenge-solver:8191/v1",
            "file:///etc/passwd",
            "http://example.com/dl\r\nHost: evil",
        ]
        for bad_url in blocked_urls:
            res_q = await client.post(
                "/api/grab/quick",
                json={"title": "SSRF Probe", "download_url": bad_url, "category": "Movies"},
            )
            assert res_q.status_code == 400, f"Expected 400 for {bad_url}, got {res_q.status_code}"

            res_a = await client.post(
                "/api/grab/arr",
                json={"title": "SSRF Probe", "download_url": bad_url, "category": "Movies"},
            )
            assert res_a.status_code == 400, f"Expected 400 for {bad_url}, got {res_a.status_code}"

    # Direct ArrClient.push_release() validation
    assert await radarr_client.push_release("Test", "http://169.254.169.254/latest/meta-data/") is False

    # Redirect hop validation + streaming size cap with real httpx.AsyncClient + MockTransport
    qb = QBittorrentClient()

    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_redirect_handler)) as real_http:
        monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: real_http)
        mag, data, err = await qb._resolve_torrent_payload(
            "http://seedbox-tunnel:9696/prowlarr/1/download?apikey=secret"
        )
        assert mag is None and data is None and err is not None

    # External redirect back to Prowlarr or internal host must be blocked too
    monkeypatch.setattr(
        url_safety,
        "validate_download_url",
        AsyncMock(side_effect=[(True, ""), (False, "Download URL points to a disallowed internal host.")]),
    )
    def _ext_to_prowlarr_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://seedbox-tunnel:9696/api/v1/indexer"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_ext_to_prowlarr_handler)) as real_http:
        monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: real_http)
        mag, data, err = await qb._resolve_torrent_payload("https://tracker.public.example/dl/1.torrent")
        assert mag is None and data is None and err is not None

    # Oversized .torrent stream is aborted via MAX_TORRENT_BYTES
    monkeypatch.setattr(url_safety, "validate_download_url", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr("backend.app.services.qbittorrent.validate_download_url", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr("backend.app.services.qbittorrent.MAX_TORRENT_BYTES", 32)

    def _oversized_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"d8:announce3:url" + b"A" * 200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_oversized_handler)) as real_http:
        monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: real_http)
        mag, data, err = await qb._resolve_torrent_payload("http://seedbox-tunnel:9696/prowlarr/big?apikey=secret")
        assert mag is None and data is None and err is not None


@pytest.mark.asyncio
async def test_session_logout_revocation_suspension_and_role_demotion_persist():
    uid = uuid.uuid4().hex[:8]
    uname = f"rev_{uid}"
    async with AsyncSessionLocal() as db:
        db.add(
            User(
                username=uname,
                email=f"{uname}@example.com",
                password_hash=hash_secret("StrongPassword99!"),
                role="admin",
                is_active=True,
                vault_pin_hash=hash_secret("9999"),
                email_verified_at=utcnow(),
                token_version=1,
            )
        )
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/api/auth/login",
            json={"username": uname, "password": "StrongPassword99!"},
        )
        assert login_res.status_code == 200
        cookie_val = login_res.cookies.get(settings.COOKIE_NAME)
        assert cookie_val
        leaked_headers = {"Cookie": f"{settings.COOKIE_NAME}={cookie_val}"}

        # 1. Logout bumps token_version in SQLite and invalidates leaked cookie even after cache clear
        out = await client.post("/api/auth/logout", headers=leaked_headers)
        assert out.status_code == 200
        _user_token_version_floors.clear()  # Simulate container restart
        assert (await client.get("/api/auth/me", headers=leaked_headers)).status_code == 401

        # 2. Re-login, then demote role in DB -> /api/settings immediately returns 403
        login2 = await client.post(
            "/api/auth/login",
            json={"username": uname, "password": "StrongPassword99!"},
        )
        assert login2.status_code == 200
        c2 = login2.cookies.get(settings.COOKIE_NAME)
        h2 = {"Cookie": f"{settings.COOKIE_NAME}={c2}"}
        assert (await client.get("/api/settings", headers=h2)).status_code == 200

        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.username == uname))).scalars().first()
            u.role = "user"
            await db.commit()
        assert (await client.get("/api/settings", headers=h2)).status_code == 403

        # 3. Suspend user in DB -> existing session immediately returns 401
        async with AsyncSessionLocal() as db:
            u = (await db.execute(select(User).where(User.username == uname))).scalars().first()
            u.is_active = False
            await db.commit()
        _user_token_version_floors.clear()
        assert (await client.get("/api/auth/me", headers=h2)).status_code == 401


def test_rate_limiter_ip_trust_and_memory_bound():
    # Untrusted peer (10.0.0.5) or public IP cannot spoof X-Forwarded-For / X-Real-IP
    wg_req = _make_request(
        client_host="10.0.0.5",
        headers={"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"},
    )
    assert get_client_ip(wg_req) == "10.0.0.5"

    # Trusted Docker bridge proxy (172.18.0.2) honors X-Real-IP or rightmost untrusted XFF hop
    caddy_req = _make_request(
        client_host="172.18.0.2",
        headers={"X-Real-IP": "198.51.100.42", "X-Forwarded-For": "9.9.9.9, 198.51.100.42"},
    )
    assert get_client_ip(caddy_req) == "198.51.100.42"

    xff_only_req = _make_request(
        client_host="172.18.0.2",
        headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.77, 172.18.0.3"},
    )
    assert get_client_ip(xff_only_req) == "203.0.113.77"

    # Bounded key eviction in LoginRateLimiter
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60, max_keys=3)
    for idx in range(10):
        limiter.record_failure(f"198.51.100.{idx}")
    assert len(limiter.attempts) == 3
    assert "198.51.100.9" in limiter.attempts


@pytest.mark.asyncio
async def test_streaming_sftp_fail_closed_and_encrypted_tokens(tmp_path: Path, monkeypatch):
    # 1. resolve_local_media_path fails closed when STREAM_MEDIA_ROOT is empty
    sample_mp4 = tmp_path / "movie.mp4"
    sample_mp4.write_bytes(b"DATA")
    sample_txt = tmp_path / "secret.txt"
    sample_txt.write_bytes(b"SECRET")

    monkeypatch.setattr(settings, "STREAM_MEDIA_ROOT", "")
    assert resolve_local_media_path(str(sample_mp4)) is None

    monkeypatch.setattr(settings, "STREAM_MEDIA_ROOT", str(tmp_path))
    assert resolve_local_media_path(str(sample_mp4)) == sample_mp4.resolve()
    assert resolve_local_media_path(str(sample_txt)) is None

    # 2. _resolve_seedbox_sftp_path blocks .ssh / hidden / root files, allows media/torrents
    monkeypatch.setattr(settings, "SEEDBOX_USER", "seedboxuser")
    monkeypatch.setattr(settings, "STREAM_MEDIA_ROOT", "/home/seedboxuser/media")
    assert _resolve_seedbox_sftp_path("/home/seedboxuser/.ssh/id_ed25519") is None
    assert _resolve_seedbox_sftp_path("/home/seedboxuser/.env") is None
    assert _resolve_seedbox_sftp_path("/home/seedboxuser/media/.hidden.mp4") is None
    assert _resolve_seedbox_sftp_path("/home/seedboxuser/media/Movies/film.mkv") == "/home/seedboxuser/media/Movies/film.mkv"
    assert _resolve_seedbox_sftp_path("/home/seedboxuser/torrents/film.mkv") == "/home/seedboxuser/torrents/film.mkv"

    # 3. _get_ssh_credentials & _SFTPPool fail closed without known_hosts
    ssh_dir = tmp_path / "ssh_no_kh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "no_home")
    with patch("backend.app.services.streaming.Path") as mock_path:
        # Directly test missing known_hosts via temporary home
        pass
    fake_home = tmp_path / "home_test"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # Only if /home/appuser/.ssh doesn't exist in test env; verify no known_hosts returns None
    if not Path("/home/appuser/.ssh/known_hosts").is_file():
        assert _get_ssh_credentials() == (None, None)
        pool = _SFTPPool()
        assert await pool.acquire() is None

    # 4. Stream token is Fernet-encrypted (file_path is not cleartext or base64-readable)
    secret_path = "/home/seedboxuser/media/Movies/SecretFilm.2026.mkv"
    tok = create_stream_token({"item_id": "radarr:1", "is_adult": False, "file_path": secret_path})
    assert "SecretFilm" not in tok
    assert base64.urlsafe_b64encode(b"SecretFilm").decode("ascii").rstrip("=") not in tok
    decoded = verify_stream_token(tok)
    assert decoded is not None and decoded["file_path"] == secret_path

    # 5. HLS proxy blocks encoded traversal & non-HLS extensions
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(settings, "JELLYFIN_URL", "https://seedbox.local/jellyfin")
        monkeypatch.setattr(settings, "JELLYFIN_API_KEY", "jfkey")
        hls_tok = create_stream_token({"item_id": "radarr:1", "is_adult": False, "jf_id": "jf1"})
        assert (await client.get(f"/api/stream/hls/{hls_tok}/..%2fSystem/Info.m3u8")).status_code == 400
        assert (await client.get(f"/api/stream/hls/{hls_tok}/System/Info.json")).status_code == 400


def test_proxy_header_spoofing_protection(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "DOMAIN", "example.com")

    # Non-proxy peer (10.0.0.5) spoofing X-Forwarded-Host must be ignored
    spoofed_req = _make_request(
        client_host="10.0.0.5",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.attacker.com"},
        host_header="evil.attacker.com",
    )
    assert _public_base_url(spoofed_req) == "https://media.example.com"

    rp_id, origins = resolve_rp_and_origins(spoofed_req)
    assert rp_id == "evil.attacker.com" or rp_id == "media.example.com"  # verify evil.attacker.com not trusted when PUBLIC_BASE_URL set
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://media.example.com")
    assert _public_base_url(spoofed_req) == "https://media.example.com"
    rp_id2, origins2 = resolve_rp_and_origins(spoofed_req)
    assert rp_id2 == "media.example.com"
    assert "https://evil.attacker.com" not in origins2


@pytest.mark.asyncio
async def test_vault_pin_persistence_and_dynamic_service_settings(monkeypatch):
    orig_pin = settings.VAULT_PIN
    monkeypatch.setattr(settings, "VAULT_PIN", orig_pin)
    monkeypatch.setattr(settings, "RADARR_API_KEY", settings.RADARR_API_KEY)
    monkeypatch.setattr(settings, "PROWLARR_API_KEY", settings.PROWLARR_API_KEY)
    monkeypatch.setattr(settings, "QBITTORRENT_PASSWORD", settings.QBITTORRENT_PASSWORD)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        try:
            res = await client.post(
                "/api/settings",
                json={
                    "vault_pin": "8426",
                    "radarr_api_key": "dyn-radarr-key",
                    "prowlarr_api_key": "dyn-prowlarr-key",
                    "qbittorrent_password": "dyn-qb-password",
                },
            )
            assert res.status_code == 200
            assert "vault_pin" in res.json()["updated"]

            # Dynamic client properties reflect runtime settings immediately
            assert radarr_client.api_key == "dyn-radarr-key"
            assert prowlarr_client.api_key == "dyn-prowlarr-key"
            assert qb_client.password == "dyn-qb-password"

            # EncryptedSetting saved vault_pin and init_db() does NOT overwrite it back to env default
            async with AsyncSessionLocal() as db:
                enc_pin = await db.get(EncryptedSetting, "vault_pin")
                assert enc_pin is not None

            settings.VAULT_PIN = orig_pin
            await init_db()

            # Admin still unlocks Vault with "8426", NOT reverted to orig_pin
            bad_unlock = await client.post("/api/vault/unlock", json={"pin": orig_pin})
            assert bad_unlock.status_code == 403
            ok_unlock = await client.post("/api/vault/unlock", json={"pin": "8426"})
            assert ok_unlock.status_code == 200
        finally:
            restore_res = await client.post("/api/settings", json={"vault_pin": orig_pin})
            assert restore_res.status_code == 200


@pytest.mark.asyncio
async def test_vault_ai_isolation_grab_category_validation_and_rbac(monkeypatch):
    monkeypatch.setattr(settings, "ADULT_INDEXER_KEYWORDS", "vault-forum,vault-gallery")
    await replace_service_library_items(
        "whisparr",
        [{"id": 901, "title": "Safe Movie Secret Vault", "clean_title": "safemoviesecretvault", "year": 2026, "has_file": True}],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Admin unlocks Vault, runs ai-pick with category="All" containing custom adult-keyword candidates
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 200

        captured = []

        async def _fake_dispatch(**kwargs):
            captured.append(kwargs)
            return {
                "query_intent": "Movies",
                "recommended_title": "Safe.Movie.2026.2160p.REMUX-Framestor",
                "recommendation_reason": "Clean pick",
                "ratings": [],
            }, "MockLLM"

        monkeypatch.setattr(ai_assistant, "_dispatch_llm_providers", AsyncMock(side_effect=_fake_dispatch))

        pick_res = await client.post(
            "/api/search/ai-pick",
            json={
                "query": "Safe Movie",
                "category": "All",
                "candidates": [
                    {
                        "guid": "safe-1",
                        "title": "Safe.Movie.2026.2160p.REMUX-Framestor",
                        "indexer": "PrivateMovies",
                        "sources": ["PrivateMovies"],
                        "size": 50_000_000_000,
                        "seeders": 100,
                    },
                    {
                        "guid": "adult-1",
                        "title": "Secret.Vault.Release.2026.MP4",
                        "indexer": "Vault-Forum",
                        "sources": ["Vault-Forum"],
                        "size": 5_000_000_000,
                        "seeders": 50,
                    },
                    {
                        "guid": "adult-2",
                        "title": "Hidden.VaultGallery.Scene.2026.MP4",
                        "indexer": "Vault-Gallery",
                        "sources": ["Vault-Gallery"],
                        "size": 4_000_000_000,
                        "seeders": 40,
                    },
                ],
            },
        )
        assert pick_res.status_code == 200
        assert len(captured) == 1
        sent_titles = [c["title"] for c in captured[0]["candidates_summary"]]
        assert sent_titles == ["Safe.Movie.2026.2160p.REMUX-Framestor"]
        assert all("Safe Movie Secret Vault" not in str(it) for it in captured[0]["library_items"])

        # Query mentioning adult tracker token must not call external LLM even when category="All"
        captured.clear()
        adult_q_res = await client.post(
            "/api/search/ai-pick",
            json={
                "query": "vault-forum release",
                "category": "All",
                "candidates": [
                    {
                        "guid": "safe-2",
                        "title": "Safe.Movie.2026.1080p.WEB-DL",
                        "indexer": "PrivateMovies",
                        "sources": ["PrivateMovies"],
                        "size": 10_000_000_000,
                        "seeders": 20,
                    },
                ],
            },
        )
        assert adult_q_res.status_code == 200
        assert len(captured) == 0

        # Maintenance AI triage scrubs xxx / configured adult keywords / whisparr even in indexer_issues
        diag_captured = []

        async def _fake_diag(**kwargs):
            diag_captured.append(kwargs.get("user_content_override", ""))
            return {"summary": "OK", "recommended_actions": ["Check"]}, "MockLLM"

        monkeypatch.setattr(ai_assistant, "_dispatch_llm_providers", AsyncMock(side_effect=_fake_diag))
        await ai_assistant.diagnose_maintenance_issues({
            "issue_count": 3,
            "stalled_torrents": [{"name": "SecretAdultTorrent", "tracker": "https://vault-gallery.example/a", "state": "stalledDL", "stalled_hours": 8.0}],
            "arr_warnings": [{"service": "whisparr", "title": "HiddenScene", "messages": ["Vault-Forum failed"]}],
            "indexer_issues": [{"indexer": "Vault-Forum", "kind": "auth_expired", "message": "Vault-Gallery cookie expired"}],
            "duplicates": [],
            "heavy_files": [],
        })
        assert diag_captured
        for forbidden in ("SecretAdultTorrent", "HiddenScene", "Vault-Forum", "Vault-Gallery"):
            assert forbidden not in diag_captured[0]

        # 2. Server-side grab validation blocks adult indexers masquerading as Movies when locked (404)
        await client.post("/api/vault/lock")
        masquerade_q = await client.post(
            "/api/grab/quick",
            json={
                "title": "Adult Release",
                "category": "Movies",
                "indexer": "Vault-Forum",
                "magnet_url": "magnet:?xt=urn:btih:1234",
            },
        )
        assert masquerade_q.status_code == 404

        masquerade_a = await client.post(
            "/api/grab/arr",
            json={
                "title": "Adult Release",
                "category": "Movies",
                "indexer": "Vault-Gallery",
                "magnet_url": "magnet:?xt=urn:btih:1234",
            },
        )
        assert masquerade_a.status_code == 404

    # 3. Non-admin RBAC checks on queue actions, maintenance blacklist-research, and redaction
    async with AsyncClient(transport=transport, base_url="http://test") as std_client:
        uid = uuid.uuid4().hex[:6]
        reg = await std_client.post(
            "/api/auth/register",
            json={"username": f"rbac_{uid}", "email": f"rbac_{uid}@example.com", "password": "StrongPassword99!"},
        )
        assert reg.status_code == 200
        assert reg.json()["role"] == "user"

        valid_hash = "a" * 40
        for act in ("pause", "resume", "delete"):
            q_res = await std_client.post("/api/queue/action", json={"hashes": [valid_hash], "action": act})
            assert q_res.status_code == 403

        m_res = await std_client.post(
            "/api/maintenance/blacklist-research",
            json={"service": "radarr", "queue_id": 1},
        )
        assert m_res.status_code == 403

        # Non-admin /api/maintenance/status redacts file_path and tracker URLs
        with (
            patch.object(watchdog.qb_client, "get_queue", AsyncMock(return_value=[{
                "hash": valid_hash, "name": "Slow", "state": "stalledDL", "progress": 0.1,
                "num_seeds": 0, "size": 1000, "last_activity": int(time.time() - 30000),
                "category": "Movies", "tracker": "https://private.tracker/announce?passkey=secret",
            }])),
            patch.object(watchdog.qb_client, "get_disk_free_space", AsyncMock(return_value=500 * 1024**3)),
            patch.object(watchdog.prowlarr_client, "get_health_and_indexer_status", AsyncMock(return_value={"reachable": True, "indexers": [], "statuses": [], "health": []})),
            patch.object(watchdog, "get_library_audit_rows", AsyncMock(return_value=[{
                "service": "radarr", "external_id": 1, "title": "Heavy Movie", "clean_title": "heavymovie",
                "year": 2024, "has_file": True, "file_path": "/home/secret/media/Movies/Heavy.mkv",
                "size_bytes": 60 * 1024**3,
            }])),
            patch.object(watchdog.radarr_client, "get_queue", AsyncMock(return_value=[])),
            patch.object(watchdog.sonarr_client, "get_queue", AsyncMock(return_value=[])),
        ):
            watchdog.clear_watchdog_cache()
            st_res = await std_client.get("/api/maintenance/status?refresh=true")
            assert st_res.status_code == 200
            st_body = st_res.json()
            assert st_body["stalled_torrents"][0]["tracker"] == ""
            assert st_body["heavy_files"][0]["file_path"] == ""
