import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from backend.app.main import app
from backend.app.config import settings
from backend.app.database import init_db


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await init_db()


@pytest.mark.asyncio
async def test_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["service"] == "media-search"


@pytest.mark.asyncio
async def test_health_endpoint_exposes_no_internal_details():
    """/api/health is public and unauthenticated, so it must not leak infra details."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert set(data) == {"status", "db_ok", "service"}
        assert "seedbox_host" not in data
        assert "gemini_model" not in data
        body = res.text
        for value in (settings.SEEDBOX_HOST, settings.SEEDBOX_USER, settings.JELLYFIN_URL):
            if value:
                assert value not in body


@pytest.mark.asyncio
async def test_auth_login_and_me():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Invalid login
        res_fail = await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        assert res_fail.status_code == 401

        # Valid login
        res_ok = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert res_ok.status_code == 200
        assert settings.COOKIE_NAME in res_ok.cookies

        # Get me
        res_me = await client.get("/api/auth/me")
        assert res_me.status_code == 200
        me_data = res_me.json()
        assert me_data["authenticated"] is True
        assert me_data["username"] == settings.ADMIN_USERNAME
        assert me_data["vault_unlocked"] is False


@pytest.mark.asyncio
async def test_adult_vault_pin_unlock_and_stealth_gating():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Log in
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )

        # 2. Check initial vault status (should be locked)
        res_status = await client.get("/api/vault/status")
        assert res_status.status_code == 200
        assert res_status.json()["unlocked"] is False

        # 3. Attempt search in Adult category while locked (Must return 404 stealth)
        res_search_denied = await client.post(
            "/api/search",
            json={"query": "test query", "category": "Adult"},
        )
        assert res_search_denied.status_code == 404

        # 4. Attempt quick grab in Adult category while locked (Must return 404 stealth)
        res_grab_denied = await client.post(
            "/api/grab/quick",
            json={"title": "Test Adult Release", "category": "adult", "magnet_url": "magnet:?xt=urn:btih:123"},
        )
        assert res_grab_denied.status_code == 404

        # 5. Unlock vault with wrong PIN
        res_wrong_pin = await client.post(
            "/api/vault/unlock",
            json={"pin": "9999"},
        )
        assert res_wrong_pin.status_code == 403

        # 6. Unlock vault with correct PIN
        res_unlock = await client.post(
            "/api/vault/unlock",
            json={"pin": settings.VAULT_PIN},
        )
        assert res_unlock.status_code == 200
        assert res_unlock.json()["unlocked"] is True

        # 7. Check vault status (now unlocked)
        res_status2 = await client.get("/api/vault/status")
        assert res_status2.json()["unlocked"] is True

        # 8. Re-lock vault
        res_lock = await client.post("/api/vault/lock")
        assert res_lock.status_code == 200
        assert res_lock.json()["unlocked"] is False


@pytest.mark.asyncio
async def test_telemetry_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Log in
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )

        res = await client.get("/api/telemetry")
        assert res.status_code == 200
        data = res.json()
        assert "services" in data
        assert "download_speed_bps" in data
        assert "seedbox_host" in data
        # In locked mode, Whisparr must be omitted from services list
        service_names = [s["name"] for s in data["services"]]
        assert "Whisparr" not in service_names
        # Admins still see the seedbox host and the Jellyfin link.
        assert data["seedbox_host"] == settings.SEEDBOX_HOST
        if settings.JELLYFIN_URL:
            assert data["jellyfin_web_url"].startswith(settings.JELLYFIN_URL.rstrip("/"))


@pytest.mark.asyncio
async def test_telemetry_hides_infra_details_from_non_admins():
    import uuid
    uid = uuid.uuid4().hex[:6]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reg = await client.post(
            "/api/auth/register",
            json={"username": f"tele_{uid}", "email": f"tele_{uid}@example.com", "password": "StrongPassword99!"},
        )
        assert reg.status_code == 200
        assert reg.json()["role"] == "user"

        res = await client.get("/api/telemetry")
        assert res.status_code == 200
        data = res.json()
        assert data["seedbox_host"] == ""
        assert data["jellyfin_web_url"] == ""
        assert all(s["url"] == "" and s["launcher_url"] == "" for s in data["services"])
        for value in (settings.SEEDBOX_HOST, settings.SEEDBOX_USER, settings.JELLYFIN_URL):
            if value:
                assert value not in res.text


def test_static_assets_contain_no_seedbox_details():
    """Everything under backend/static is served without auth, so it must not name the seedbox."""
    from pathlib import Path

    static_root = Path(__file__).resolve().parent.parent / "backend" / "static"
    secrets = [v for v in (settings.SEEDBOX_HOST, settings.SEEDBOX_USER) if v]
    assert secrets, "test needs SEEDBOX_HOST/SEEDBOX_USER to have values"
    offenders = []
    for path in static_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".js", ".css", ".json", ".svg", ".webmanifest", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [str(path.relative_to(static_root)) for v in secrets if v in text]
    assert not offenders, f"seedbox host/user found in public static files: {sorted(set(offenders))}"


@pytest.mark.asyncio
async def test_search_suggestions_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Log in first
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )

        res = await client.get("/api/search/suggestions?q=inter")
        assert res.status_code == 200
        data = res.json()
        assert "suggestions" in data
        assert len(data["suggestions"]) > 0
        titles = [s["title"] for s in data["suggestions"]]
        assert "Interstellar" in titles


@pytest.mark.asyncio
async def test_registration_and_email_login():
    import uuid
    uid = uuid.uuid4().hex[:6]
    uname = f"tester_{uid}"
    email = f"tester_{uid}@example.com"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Register new user
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "username": uname,
                "email": email,
                "password": "Password123!",
            },
        )
        assert reg_res.status_code == 200
        data = reg_res.json()
        assert data["success"] is True
        assert data["username"] == uname
        assert data["email"] == email
        assert data["role"] == "user"

        # Logout
        await client.post("/api/auth/logout")

        # Login with email
        login_res = await client.post(
            "/api/auth/login",
            json={"username": email, "password": "Password123!"},
        )
        assert login_res.status_code == 200
        assert login_res.json()["role"] == "user"

        # Check me
        me_res = await client.get("/api/auth/me")
        assert me_res.status_code == 200
        assert me_res.json()["email"] == email
        assert me_res.json()["role"] == "user"


@pytest.mark.asyncio
async def test_role_rbac_and_logged_off_lockdown():
    import uuid
    uid = uuid.uuid4().hex[:6]
    uname = f"std_{uid}"
    email = f"std_{uid}@example.com"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Logged-off visitor cannot search
        search_unauth = await client.post(
            "/api/search",
            json={"query": "Inception"},
        )
        assert search_unauth.status_code == 401

        # 2. Logged-off visitor cannot access settings
        settings_unauth = await client.get("/api/settings")
        assert settings_unauth.status_code == 401

        # 3. Standard user registers and is logged in
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "username": uname,
                "email": email,
                "password": "StrongPassword99!",
            },
        )
        assert reg_res.status_code == 200

        # Attempt to access admin settings -> must be 403 Forbidden
        settings_denied = await client.get("/api/settings")
        assert settings_denied.status_code == 403

        # 4. Admin logs in -> must be 200 OK
        await client.post("/api/auth/logout")
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        settings_ok = await client.get("/api/settings")
        assert settings_ok.status_code == 200


@pytest.mark.asyncio
async def test_qbittorrent_add_torrent_fetches_http_payload_and_handles_redirects(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from backend.app.services.qbittorrent import QBittorrentClient

    qb = QBittorrentClient()
    torrent_bytes = b"d8:announce21:https://tracker.local4:infod4:name4:testee"

    mock_dl = MagicMock(status_code=200, content=torrent_bytes, headers={})
    mock_add = MagicMock(status_code=200, text="Ok.")

    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=mock_dl)
    fake_client.post = AsyncMock(return_value=mock_add)
    monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: fake_client)

    ok = await qb.add_torrent("http://seedbox-tunnel:9696/prowlarr/1/download?apikey=x", category="Movies")
    assert ok is True
    fake_client.get.assert_awaited_once()
    post_kwargs = fake_client.post.call_args.kwargs
    assert "torrents" in post_kwargs["files"]
    assert post_kwargs["files"]["torrents"][1] == torrent_bytes
    assert post_kwargs["data"]["stopped"] == "false"

    # Reject non-bencoded HTML error page
    fake_client.get = AsyncMock(return_value=MagicMock(status_code=200, content=b"<html>404</html>", headers={}))
    fake_client.post.reset_mock()
    failed = await qb.add_torrent("http://seedbox-tunnel:9696/prowlarr/1/download?apikey=bad")
    assert failed is False
    fake_client.post.assert_not_awaited()

    # Follow 302 -> magnet: redirect cleanly
    fake_client.get = AsyncMock(
        return_value=MagicMock(status_code=302, content=b"", headers={"location": "magnet:?xt=urn:btih:ABCDEF"})
    )
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text="Ok."))
    ok_magnet = await qb.add_torrent("http://seedbox-tunnel:9696/prowlarr/1/download?apikey=mag")
    assert ok_magnet is True
    assert fake_client.post.call_args.kwargs["data"]["urls"] == "magnet:?xt=urn:btih:ABCDEF"


@pytest.mark.asyncio
async def test_qbittorrent_control_torrents_v5_and_v4_fallback(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from backend.app.services.qbittorrent import QBittorrentClient

    qb = QBittorrentClient()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        side_effect=[
            MagicMock(status_code=404),  # /torrents/stop -> 404 on v4
            MagicMock(status_code=200),  # /torrents/pause -> 200 fallback
        ]
    )
    monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: fake_client)

    ok = await qb.control_torrents("pause", ["abc123"])
    assert ok is True
    assert fake_client.post.call_count == 2
    assert fake_client.post.call_args_list[0].args[0].endswith("/api/v2/torrents/stop")
    assert fake_client.post.call_args_list[1].args[0].endswith("/api/v2/torrents/pause")


@pytest.mark.asyncio
async def test_qbittorrent_timeout_magnet_and_infohash_fallback(monkeypatch, caplog):
    import logging
    from unittest.mock import AsyncMock, MagicMock
    import httpx
    from backend.app.services.qbittorrent import QBittorrentClient

    qb = QBittorrentClient()
    fake_client = MagicMock()
    monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: fake_client)

    # 1. Empty ReadTimeout -> falls back cleanly to explicit magnet_url and logs exception type
    fake_client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text="Ok."))

    with caplog.at_level(logging.WARNING):
        ok, err = await qb.add_torrent_with_result(
            urls="http://seedbox-tunnel:9696/prowlarr/5/download?apikey=x",
            category="Movies",
            magnet_url="magnet:?xt=urn:btih:DEADBEEF1234",
            title="Test Movie",
        )

    assert ok is True
    assert err is None
    assert fake_client.get.await_count == 2
    get_timeout = fake_client.get.call_args.kwargs["timeout"]
    assert isinstance(get_timeout, httpx.Timeout)
    assert get_timeout.read == 18.0
    assert get_timeout.connect == 4.0
    assert fake_client.post.call_args.kwargs["data"]["urls"] == "magnet:?xt=urn:btih:DEADBEEF1234"
    assert "ReadTimeout" in caplog.text

    # 2. HTML payload instead of bencoded .torrent -> builds magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}
    fake_client.get = AsyncMock(return_value=MagicMock(status_code=200, content=b"<html>Cloudflare</html>", headers={}))
    fake_client.post = AsyncMock(return_value=MagicMock(status_code=200, text="Ok."))

    ok_ih, err_ih = await qb.add_torrent_with_result(
        urls="http://seedbox-tunnel:9696/prowlarr/9/download?apikey=x",
        category="Movies",
        info_hash="0123456789ABCDEF",
        title="Dune Part Two",
    )
    assert ok_ih is True
    assert err_ih is None
    assert (
        fake_client.post.call_args.kwargs["data"]["urls"]
        == "magnet:?xt=urn:btih:0123456789ABCDEF&dn=Dune%20Part%20Two"
    )


@pytest.mark.asyncio
async def test_quick_grab_error_messages_distinguish_prowlarr_vs_qbittorrent(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import httpx

    fake_client = MagicMock()
    monkeypatch.setattr("backend.app.services.qbittorrent.get_search_client", lambda: fake_client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login_res.status_code == 200

        # Case A: Prowlarr .torrent download times out and no magnet/infoHash fallback exists
        fake_client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        fake_client.post = AsyncMock()
        res_prowlarr_fail = await client.post(
            "/api/grab/quick",
            json={
                "title": "Slow Indexer Release",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/7/download?apikey=x",
                "category": "Movies",
                "indexer": "PrivateTracker",
                "size": 1024,
            },
        )
        assert res_prowlarr_fail.status_code == 422
        assert (
            res_prowlarr_fail.json()["detail"]
            == "Prowlarr .torrent download failed (and no magnet/infoHash fallback available)."
        )
        assert fake_client.get.await_count == 2
        fake_client.post.assert_not_awaited()

        # Case B: Magnet fallback / payload resolved, but qBittorrent is unreachable
        fake_client.get = AsyncMock(side_effect=httpx.ReadTimeout(""))
        fake_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        res_qb_fail = await client.post(
            "/api/grab/quick",
            json={
                "title": "Unreachable Tunnel Release",
                "download_url": "http://seedbox-tunnel:9696/prowlarr/7/download?apikey=x",
                "info_hash": "AABBCCDDEEFF0011",
                "category": "Movies",
                "indexer": "PrivateMovies",
                "size": 2048,
            },
        )
        assert res_qb_fail.status_code == 502
        assert res_qb_fail.json()["detail"] == "qBittorrent is unreachable."


@pytest.mark.asyncio
async def test_vault_unlock_lock_preserves_admin_role_and_blocks_user_fallback():
    import uuid
    uid = uuid.uuid4().hex[:6]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Admin logs in, unlocks vault, locks vault -> must keep role='admin' and /api/settings access
        await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        unlock_res = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock_res.status_code == 200
        assert (await client.get("/api/auth/me")).json()["role"] == "admin"
        assert (await client.get("/api/settings")).status_code == 200

        lock_res = await client.post("/api/vault/lock")
        assert lock_res.status_code == 200
        assert (await client.get("/api/auth/me")).json()["role"] == "admin"
        assert (await client.get("/api/settings")).status_code == 200

        # 2. Standard user must NOT unlock vault with global admin VAULT_PIN fallback or delete disk files
        await client.post("/api/auth/logout")
        reg = await client.post(
            "/api/auth/register",
            json={"username": f"usr_{uid}", "email": f"usr_{uid}@example.com", "password": "StrongPassword99!"},
        )
        assert reg.status_code == 200
        assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 403
        assert (
            await client.post(
                "/api/queue/action",
                json={"hashes": ["abc123"], "action": "delete", "delete_files": True},
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_registration_gating_invite_code_and_login_pages(monkeypatch):
    import uuid
    uid = uuid.uuid4().hex[:6]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cfg_res = await client.get("/api/auth/config")
        assert cfg_res.status_code == 200
        assert "allow_registration" in cfg_res.json()
        assert "vault_timeout_minutes" in cfg_res.json()

        login_page = await client.get("/login")
        assert login_page.status_code == 200
        assert (
            ('id="root"' in login_page.text and "/static/app/" in login_page.text)
            or ("Forgot password?" in login_page.text and "auth.js?v=" in login_page.text)
        )

        # Closed registration
        monkeypatch.setattr(settings, "ALLOW_REGISTRATION", False)
        closed_res = await client.post(
            "/api/auth/register",
            json={"username": f"closed_{uid}", "email": f"closed_{uid}@example.com", "password": "StrongPassword99!"},
        )
        assert closed_res.status_code == 403

        # Invite-only registration
        monkeypatch.setattr(settings, "ALLOW_REGISTRATION", True)
        monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "MUUVIES-INVITE-2026")
        bad_inv = await client.post(
            "/api/auth/register",
            json={"username": f"inv_{uid}", "email": f"inv_{uid}@example.com", "password": "StrongPassword99!", "invite_code": "wrong"},
        )
        assert bad_inv.status_code == 403

        good_inv = await client.post(
            "/api/auth/register",
            json={"username": f"inv_{uid}", "email": f"inv_{uid}@example.com", "password": "StrongPassword99!", "invite_code": "MUUVIES-INVITE-2026"},
        )
        assert good_inv.status_code == 200


@pytest.mark.asyncio
async def test_email_verification_flow_allowlist_and_rate_limit(monkeypatch):
    import uuid
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlparse
    from sqlalchemy import select
    from backend.app.database import AsyncSessionLocal
    from backend.app.models import User
    from backend.app.routers import auth as auth_router
    from backend.app.security import resend_limiter

    uid = uuid.uuid4().hex[:6]
    allowed_email = f"Allowed_{uid}@example.com"
    blocked_email = f"blocked_{uid}@example.com"
    sent_calls = []

    async def fake_send_verification_email(to_email: str, verify_url: str) -> bool:
        sent_calls.append((to_email, verify_url))
        return True

    monkeypatch.setattr(auth_router, "send_verification_email", fake_send_verification_email)
    monkeypatch.setattr(settings, "ALLOW_REGISTRATION", True)
    monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "")
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", True)
    monkeypatch.setattr(settings, "ALLOWED_EMAILS", f"other@example.com, {allowed_email}")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://media.example.com")
    resend_limiter.attempts.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Non-allowlisted email rejected cleanly with 403
        blocked_res = await client.post(
            "/api/auth/register",
            json={"username": f"blk_{uid}", "email": blocked_email, "password": "StrongPassword99!"},
        )
        assert blocked_res.status_code == 403
        assert len(sent_calls) == 0

        # 2. Allowlisted email (case-insensitive) registers, sends email, NO session cookie issued
        reg_res = await client.post(
            "/api/auth/register",
            json={"username": f"ver_{uid}", "email": allowed_email.lower(), "password": "StrongPassword99!"},
        )
        assert reg_res.status_code == 200
        reg_data = reg_res.json()
        assert reg_data["status"] == "verification_sent"
        assert reg_data["email"] == allowed_email.lower()
        assert settings.COOKIE_NAME not in reg_res.cookies
        assert (await client.get("/api/auth/me")).status_code == 401
        assert len(sent_calls) == 1
        assert sent_calls[0][1].startswith("https://media.example.com/verify-email?token=")

        first_token = parse_qs(urlparse(sent_calls[0][1]).query)["token"][0]

        # 3. Login blocked with 403 EMAIL_UNVERIFIED until verified (wrong password remains 401)
        bad_pw_res = await client.post(
            "/api/auth/login",
            json={"username": f"ver_{uid}", "password": "WrongPassword99!"},
        )
        assert bad_pw_res.status_code == 401

        unverified_login = await client.post(
            "/api/auth/login",
            json={"username": f"ver_{uid}", "password": "StrongPassword99!"},
        )
        assert unverified_login.status_code == 403
        assert unverified_login.json() == {"detail": "Email not verified", "code": "EMAIL_UNVERIFIED"}

        # 4. Anti-enumeration on unknown email resend + rate-limit (1 per 120s per IP/email)
        unknown_resend = await client.post(
            "/api/auth/resend-verification",
            json={"email": f"nobody_{uid}@example.com"},
        )
        assert unknown_resend.status_code == 200

        resend_ok = await client.post(
            "/api/auth/resend-verification",
            json={"email": allowed_email},
        )
        assert resend_ok.status_code == 200
        assert len(sent_calls) == 2
        fresh_token = parse_qs(urlparse(sent_calls[1][1]).query)["token"][0]
        assert fresh_token != first_token

        resend_limited = await client.post(
            "/api/auth/resend-verification",
            json={"email": allowed_email},
        )
        assert resend_limited.status_code == 429

        # 5. Expired token must be rejected via POST /api/auth/verify-email
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.email == allowed_email.lower()))).scalars().first()
            assert db_user is not None
            saved_exp = db_user.verification_expires_at
            db_user.verification_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            await db.commit()

        expired_res = await client.post("/api/auth/verify-email", json={"token": fresh_token})
        assert expired_res.status_code == 400

        # Restore fresh token + valid expiry and verify via GET /verify-email
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.email == allowed_email.lower()))).scalars().first()
            assert db_user.verification_token_hash is None
            import hashlib
            db_user.verification_token_hash = hashlib.sha256(fresh_token.encode("utf-8")).hexdigest()
            db_user.verification_expires_at = saved_exp
            await db.commit()

        verify_get = await client.get(f"/verify-email?token={fresh_token}", follow_redirects=False)
        assert verify_get.status_code == 302
        assert verify_get.headers["location"] == "/login?verified=1"
        assert settings.COOKIE_NAME in verify_get.cookies

        # 6. Token is single-use and authenticated session now works; login also succeeds
        reuse_get = await client.get(f"/verify-email?token={fresh_token}", follow_redirects=False)
        assert reuse_get.status_code == 302
        assert reuse_get.headers["location"] == "/login?verify_error=1"

        me_res = await client.get("/api/auth/me")
        assert me_res.status_code == 200
        assert me_res.json()["username"] == f"ver_{uid}"

        await client.post("/api/auth/logout")
        verified_login = await client.post(
            "/api/auth/login",
            json={"username": f"ver_{uid}", "password": "StrongPassword99!"},
        )
        assert verified_login.status_code == 200


@pytest.mark.asyncio
async def test_email_service_resend_api_call_and_dev_fallback(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from backend.app.services import email_service

    # Dev fallback when RESEND_API_KEY is empty returns True cleanly without HTTP call
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    assert await email_service.send_verification_email("user@example.com", "https://media.example.com/verify-email?token=abc") is True

    # Live Resend call uses shared HTTPX pool with Bearer token
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = '{"id": "email_123"}'
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)

    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_123")
    monkeypatch.setattr(settings, "EMAIL_FROM", "Muuvies <noreply@example.com>")
    monkeypatch.setattr(email_service, "get_search_client", lambda: fake_client)

    ok = await email_service.send_verification_email("user@example.com", "https://media.example.com/verify-email?token=xyz")
    assert ok is True
    fake_client.post.assert_awaited_once()
    call_args, call_kwargs = fake_client.post.call_args
    assert call_args[0] == "https://api.resend.com/emails"
    assert call_kwargs["headers"]["Authorization"] == "Bearer re_test_123"
    assert call_kwargs["json"]["to"] == ["user@example.com"]
    assert "https://media.example.com/verify-email?token=xyz" in call_kwargs["json"]["html"]


@pytest.mark.asyncio
async def test_forgot_and_reset_password_invalidates_existing_sessions(monkeypatch):
    import hashlib
    import uuid
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs, urlparse
    from sqlalchemy import select
    from backend.app.database import AsyncSessionLocal
    from backend.app.models import User
    from backend.app.routers import auth as auth_router
    from backend.app.security import resend_limiter

    uid = uuid.uuid4().hex[:6]
    user_email = f"reset_{uid}@example.com"
    sent_resets = []

    async def fake_send_reset_email(to_email: str, reset_url: str) -> bool:
        sent_resets.append((to_email, reset_url))
        return True

    monkeypatch.setattr(auth_router, "send_password_reset_email", fake_send_reset_email)
    monkeypatch.setattr(settings, "ALLOW_REGISTRATION", True)
    monkeypatch.setattr(settings, "REGISTRATION_INVITE_CODE", "")
    monkeypatch.setattr(settings, "REQUIRE_EMAIL_VERIFICATION", False)
    monkeypatch.setattr(settings, "ALLOWED_EMAILS", "")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://media.example.com")
    resend_limiter.attempts.clear()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Register user and retain session cookie before reset
        reg = await client.post(
            "/api/auth/register",
            json={"username": f"rst_{uid}", "email": user_email, "password": "OldPassword99!"},
        )
        assert reg.status_code == 200
        old_cookie = reg.cookies.get(settings.COOKIE_NAME)
        assert old_cookie
        assert (await client.get("/api/auth/me", headers={"Cookie": f"{settings.COOKIE_NAME}={old_cookie}"})).status_code == 200

        # 2. Reset page HTML route serves login/reset shell cleanly
        reset_page = await client.get("/reset-password?token=sample")
        assert reset_page.status_code == 200
        assert (
            ('id="root"' in reset_page.text and "/static/app/" in reset_page.text)
            or "Confirm new password" in reset_page.text
        )

        # 3. Unknown email returns generic 200 without enumeration
        unknown_fp = await client.post(
            "/api/auth/forgot-password",
            json={"email": f"nobody_{uid}@example.com"},
        )
        assert unknown_fp.status_code == 200
        assert len(sent_resets) == 0

        # 4. Known email gets 30-min reset token & rate-limits repeat within 120s
        fp_ok = await client.post("/api/auth/forgot-password", json={"email": user_email})
        assert fp_ok.status_code == 200
        assert len(sent_resets) == 1
        assert sent_resets[0][1].startswith("https://media.example.com/reset-password?token=")
        reset_token = parse_qs(urlparse(sent_resets[0][1]).query)["token"][0]

        fp_limited = await client.post("/api/auth/forgot-password", json={"email": user_email})
        assert fp_limited.status_code == 429

        # 5. Expired reset token must fail cleanly with 400 and clear token
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.email == user_email))).scalars().first()
            saved_exp = db_user.reset_expires_at
            db_user.reset_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            await db.commit()

        exp_res = await client.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "new_password": "NewPassword99!"},
        )
        assert exp_res.status_code == 400

        # Restore valid expiry + token hash and complete reset
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.email == user_email))).scalars().first()
            assert db_user.reset_token_hash is None
            db_user.reset_token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
            db_user.reset_expires_at = saved_exp
            await db.commit()

        reset_ok = await client.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "new_password": "NewPassword99!"},
        )
        assert reset_ok.status_code == 200

        # 6. Pre-existing session cookie must be invalidated (token_version bumped 1 -> 2)
        stale_me = await client.get("/api/auth/me", headers={"Cookie": f"{settings.COOKIE_NAME}={old_cookie}"})
        assert stale_me.status_code == 401

        # 7. Old password rejected, new password succeeds
        old_login = await client.post(
            "/api/auth/login",
            json={"username": f"rst_{uid}", "password": "OldPassword99!"},
        )
        assert old_login.status_code == 401

        new_login = await client.post(
            "/api/auth/login",
            json={"username": f"rst_{uid}", "password": "NewPassword99!"},
        )
        assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_login_and_register_server_redirect_for_authenticated_session():
    """GET /login and GET /register return 302 -> / for valid active sessions, 200 otherwise."""
    import uuid
    from sqlalchemy import select
    from backend.app.config import settings
    from backend.app.database import AsyncSessionLocal
    from backend.app.models import User, utcnow
    from backend.app.security import hash_secret

    uid = uuid.uuid4().hex[:8]
    username = f"redir_{uid}"

    async with AsyncSessionLocal() as db:
        db.add(
            User(
                username=username,
                email=f"{username}@example.com",
                password_hash=hash_secret("Pass1234!"),
                role="user",
                is_active=True,
                email_verified_at=utcnow(),
                token_version=1,
            )
        )
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated requests render login/register page (200)
        unauth_login = await client.get("/login", follow_redirects=False)
        unauth_reg = await client.get("/register", follow_redirects=False)
        assert unauth_login.status_code == 200
        assert unauth_reg.status_code == 200

        # Sign in and obtain valid session cookie
        login_res = await client.post(
            "/api/auth/login",
            json={"username": username, "password": "Pass1234!"},
        )
        assert login_res.status_code == 200
        cookie_val = login_res.cookies.get(settings.COOKIE_NAME)
        assert cookie_val
        cookie_headers = {"Cookie": f"{settings.COOKIE_NAME}={cookie_val}"}

        # Authenticated session on /login and /register redirects immediately (302 -> /)
        auth_login = await client.get("/login", headers=cookie_headers, follow_redirects=False)
        auth_reg = await client.get("/register", headers=cookie_headers, follow_redirects=False)
        assert auth_login.status_code == 302
        assert auth_login.headers.get("location") == "/"
        assert auth_reg.status_code == 302
        assert auth_reg.headers.get("location") == "/"

        # /reset-password still renders 200 even when authenticated
        auth_reset = await client.get(
            "/reset-password?token=abc",
            headers=cookie_headers,
            follow_redirects=False,
        )
        assert auth_reset.status_code == 200

        # Bump token_version to revoke the session cookie -> falls back to 200
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.username == username))).scalars().first()
            db_user.token_version += 1
            await db.commit()

        revoked_login = await client.get("/login", headers=cookie_headers, follow_redirects=False)
        revoked_reg = await client.get("/register", headers=cookie_headers, follow_redirects=False)
        assert revoked_login.status_code == 200
        assert revoked_reg.status_code == 200

        # Restore token_version but deactivate user -> also falls back to 200
        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(select(User).where(User.username == username))).scalars().first()
            db_user.token_version = 1
            db_user.is_active = False
            await db.commit()

        inactive_login = await client.get("/login", headers=cookie_headers, follow_redirects=False)
        assert inactive_login.status_code == 200


@pytest.mark.asyncio
async def test_webauthn_passkeys_login_and_biometric_vault_unlock():
    """End-to-end WebAuthn passkey registration, login (sign_count + replay + token_version), and stealth 404 Vault unlock."""
    import hashlib
    import json
    import os
    import struct
    import uuid
    import cbor2
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from sqlalchemy import select
    from webauthn.helpers import bytes_to_base64url

    from backend.app.database import AsyncSessionLocal
    from backend.app.models import AuditLog, User, UserPasskey, utcnow
    from backend.app.security import hash_secret

    private_key = ec.generate_private_key(ec.SECP256R1())
    pub_nums = private_key.public_key().public_numbers()
    cose_key = cbor2.dumps(
        {
            1: 2,  # kty: EC2
            3: -7,  # alg: ES256
            -1: 1,  # crv: P-256
            -2: pub_nums.x.to_bytes(32, "big"),
            -3: pub_nums.y.to_bytes(32, "big"),
        }
    )
    cred_id_bytes = os.urandom(32)
    cred_id_b64 = bytes_to_base64url(cred_id_bytes)
    aaguid = b"\x00" * 16

    def _make_reg_credential(challenge_b64: str, rp_id: str, origin: str, transports=None) -> dict:
        rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = 0x01 | 0x04 | 0x40  # UP | UV | AT
        attested = aaguid + struct.pack(">H", len(cred_id_bytes)) + cred_id_bytes + cose_key
        auth_data = rp_hash + struct.pack(">BI", flags, 0) + attested
        attestation_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": challenge_b64, "origin": origin}
        ).encode("utf-8")
        return {
            "id": cred_id_b64,
            "rawId": cred_id_b64,
            "type": "public-key",
            "transports": transports or [],
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_obj),
                "transports": transports or [],
            },
        }

    def _make_auth_credential(
        challenge_b64: str,
        rp_id: str,
        origin: str,
        *,
        sign_count: int,
        user_verified: bool = True,
    ) -> dict:
        rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
        flags = 0x01 | (0x04 if user_verified else 0x00)
        auth_data = rp_hash + struct.pack(">BI", flags, sign_count)
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": challenge_b64, "origin": origin}
        ).encode("utf-8")
        signed_payload = auth_data + hashlib.sha256(client_data).digest()
        signature = private_key.sign(signed_payload, ec.ECDSA(hashes.SHA256()))
        return {
            "id": cred_id_b64,
            "rawId": cred_id_b64,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }

    uid = uuid.uuid4().hex[:8]
    std_username = f"pkstd_{uid}"
    vault_username = f"pkvault_{uid}"
    async with AsyncSessionLocal() as db:
        db.add(
            User(
                username=std_username,
                email=f"{std_username}@example.com",
                password_hash=hash_secret("Pass1234!"),
                role="user",
                is_active=True,
                vault_pin_hash="",
                email_verified_at=utcnow(),
                token_version=1,
            )
        )
        db.add(
            User(
                username=vault_username,
                email=f"{vault_username}@example.com",
                password_hash=hash_secret("Pass1234!"),
                role="admin",
                is_active=True,
                vault_pin_hash=hash_secret("4321"),
                email_verified_at=utcnow(),
                token_version=1,
            )
        )
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Vault passkey endpoints preserve 404 stealth when unauthenticated
        assert (await client.post("/api/vault/unlock/passkey/options")).status_code == 404
        assert (
            await client.post(
                "/api/vault/unlock/passkey/verify",
                json={"challenge_id": "invalid-cid", "credential": {"id": cred_id_b64}},
            )
        ).status_code == 404

        # Standard user without Vault access also receives 404 stealth
        std_login = await client.post(
            "/api/auth/login",
            json={"username": std_username, "password": "Pass1234!"},
        )
        assert std_login.status_code == 200
        assert (await client.post("/api/vault/unlock/passkey/options")).status_code == 404
        await client.post("/api/auth/logout")

        # 2. Vault-eligible user signs in and registers a passkey (with mixed known + unrecognized transport for 1P safety)
        assert (await client.post("/api/auth/passkeys/register/options")).status_code == 401
        adm_login = await client.post(
            "/api/auth/login",
            json={"username": vault_username, "password": "Pass1234!"},
        )
        assert adm_login.status_code == 200

        reg_opt_res = await client.post("/api/auth/passkeys/register/options")
        assert reg_opt_res.status_code == 200
        reg_opt_data = reg_opt_res.json()
        assert reg_opt_data["options"]["attestation"] == "none"
        assert reg_opt_data["options"]["rp"]["id"] == "test"

        reg_cred = _make_reg_credential(
            reg_opt_data["options"]["challenge"],
            "test",
            "http://test",
            transports=["internal", "custom-1p-unknown"],
        )
        reg_ver_res = await client.post(
            "/api/auth/passkeys/register/verify",
            json={
                "challenge_id": reg_opt_data["challenge_id"],
                "credential": reg_cred,
                "device_name": "1Password MacBook",
            },
        )
        assert reg_ver_res.status_code == 200
        pk_info = reg_ver_res.json()
        assert pk_info["device_name"] == "1Password MacBook"
        assert pk_info["transports"] == ["internal"]
        passkey_id = pk_info["id"]

        # Single-use challenge replay on registration must fail
        replay_reg = await client.post(
            "/api/auth/passkeys/register/verify",
            json={
                "challenge_id": reg_opt_data["challenge_id"],
                "credential": reg_cred,
                "device_name": "Replay",
            },
        )
        assert replay_reg.status_code == 400

        # Duplicate credential_id with a fresh challenge returns 409 Conflict
        reg_opt_2 = (await client.post("/api/auth/passkeys/register/options")).json()
        dup_cred = _make_reg_credential(reg_opt_2["options"]["challenge"], "test", "http://test")
        dup_res = await client.post(
            "/api/auth/passkeys/register/verify",
            json={"challenge_id": reg_opt_2["challenge_id"], "credential": dup_cred, "device_name": "Dup"},
        )
        assert dup_res.status_code == 409

        list_res = await client.get("/api/auth/passkeys")
        assert list_res.status_code == 200
        assert len(list_res.json()["passkeys"]) >= 1

        # 3. Passwordless login flow + sign_count increment + challenge replay protection + token_version
        await client.post("/api/auth/logout")
        login_opt_res = await client.post("/api/auth/passkeys/login/options")
        assert login_opt_res.status_code == 200
        login_opt = login_opt_res.json()

        auth_cred_1 = _make_auth_credential(
            login_opt["options"]["challenge"],
            "test",
            "http://test",
            sign_count=1,
            user_verified=True,
        )
        login_ver_res = await client.post(
            "/api/auth/passkeys/login/verify",
            json={"challenge_id": login_opt["challenge_id"], "credential": auth_cred_1},
        )
        assert login_ver_res.status_code == 200
        assert login_ver_res.json()["username"] == vault_username
        first_cookie = login_ver_res.cookies.get(settings.COOKIE_NAME)
        assert first_cookie

        async with AsyncSessionLocal() as db:
            db_pk = (await db.execute(select(UserPasskey).where(UserPasskey.id == passkey_id))).scalars().first()
            assert db_pk.sign_count == 1
            assert db_pk.last_used_at is not None

        # Challenge replay must fail
        replay_login = await client.post(
            "/api/auth/passkeys/login/verify",
            json={"challenge_id": login_opt["challenge_id"], "credential": auth_cred_1},
        )
        assert replay_login.status_code == 400

        # Cloned / downgraded sign_count (<= 1) must be rejected with 401
        stale_opt = (await client.post("/api/auth/passkeys/login/options")).json()
        stale_cred = _make_auth_credential(
            stale_opt["options"]["challenge"],
            "test",
            "http://test",
            sign_count=1,
            user_verified=True,
        )
        assert (
            await client.post(
                "/api/auth/passkeys/login/verify",
                json={"challenge_id": stale_opt["challenge_id"], "credential": stale_cred},
            )
        ).status_code == 401

        # Token version bump invalidates session issued via passkey
        async with AsyncSessionLocal() as db:
            vault_user = (await db.execute(select(User).where(User.username == vault_username))).scalars().first()
            orig_ver = int(vault_user.token_version or 1)
            vault_user.token_version = orig_ver + 1
            await db.commit()

        assert (
            await client.get("/api/auth/me", headers={"Cookie": f"{settings.COOKIE_NAME}={first_cookie}"})
        ).status_code == 401

        # Re-login with passkey (sign_count = 2) restores valid session at bumped token_version
        login_opt_2 = (await client.post("/api/auth/passkeys/login/options")).json()
        auth_cred_2 = _make_auth_credential(
            login_opt_2["options"]["challenge"],
            "test",
            "http://test",
            sign_count=2,
            user_verified=True,
        )
        relogin_res = await client.post(
            "/api/auth/passkeys/login/verify",
            json={"challenge_id": login_opt_2["challenge_id"], "credential": auth_cred_2},
        )
        assert relogin_res.status_code == 200
        assert (await client.get("/api/auth/me")).status_code == 200

        # 4. Biometric Adult Vault unlock enforces user_verification=True
        v_opt_res = await client.post("/api/vault/unlock/passkey/options")
        assert v_opt_res.status_code == 200
        v_opt = v_opt_res.json()
        assert v_opt["options"]["userVerification"] == "required"

        # UV=False assertion must be rejected with 403
        no_uv_cred = _make_auth_credential(
            v_opt["options"]["challenge"],
            "test",
            "http://test",
            sign_count=3,
            user_verified=False,
        )
        assert (
            await client.post(
                "/api/vault/unlock/passkey/verify",
                json={"challenge_id": v_opt["challenge_id"], "credential": no_uv_cred},
            )
        ).status_code == 403

        # UV=True assertion unlocks the Adult Vault session
        v_opt_2 = (await client.post("/api/vault/unlock/passkey/options")).json()
        uv_cred = _make_auth_credential(
            v_opt_2["options"]["challenge"],
            "test",
            "http://test",
            sign_count=3,
            user_verified=True,
        )
        v_ver_res = await client.post(
            "/api/vault/unlock/passkey/verify",
            json={"challenge_id": v_opt_2["challenge_id"], "credential": uv_cred},
        )
        assert v_ver_res.status_code == 200
        assert v_ver_res.json()["unlocked"] is True
        assert (await client.get("/api/vault/status")).json()["unlocked"] is True

        # 5. Delete passkey + AuditLog verification
        del_res = await client.delete(f"/api/auth/passkeys/{passkey_id}")
        assert del_res.status_code == 200
        assert del_res.json()["deleted_id"] == passkey_id

        async with AsyncSessionLocal() as db:
            events = (await db.execute(select(AuditLog.event))).scalars().all()
            assert "passkey_registered" in events
            assert "passkey_login" in events
            assert "vault_unlock_passkey" in events
            assert "passkey_deleted" in events


@pytest.mark.asyncio
async def test_queue_action_hash_validation_and_vault_scoping(monkeypatch):
    from unittest.mock import AsyncMock
    from backend.app.routers import queue as queue_router

    movie_hash = "0123456789abcdef" * 2 + "01234567"  # 40-char lowercase hex
    adult_hash = "fedcba9876543210" * 4  # 64-char lowercase hex
    missing_hash = "a" * 40

    sample_queue = [
        {
            "hash": movie_hash,
            "name": "Public.Movie.2026.1080p",
            "category": "Movies",
            "size": 1024,
            "progress": 0.5,
            "dlspeed": 100,
            "upspeed": 0,
            "eta": 60,
            "state": "downloading",
            "num_seeds": 5,
            "num_leechs": 1,
        },
        {
            "hash": adult_hash,
            "name": "Hidden.Adult.Release",
            "category": "adult",
            "size": 2048,
            "progress": 1.0,
            "dlspeed": 0,
            "upspeed": 50,
            "eta": 8640000,
            "state": "uploading",
            "num_seeds": 2,
            "num_leechs": 0,
        },
    ]

    get_queue_mock = AsyncMock(return_value=sample_queue)
    control_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(queue_router.qb_client, "get_queue", get_queue_mock)
    monkeypatch.setattr(queue_router.qb_client, "control_torrents", control_mock)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_res = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login_res.status_code == 200

        # 1. Reject "all" (and case variants)
        for bad_all in ("all", "ALL"):
            res_all = await client.post(
                "/api/queue/action",
                json={"hashes": [bad_all], "action": "pause"},
            )
            assert res_all.status_code == 400
        control_mock.assert_not_awaited()

        # 2. Reject malformed hashes (wrong length, non-hex, uppercase, pipe injection)
        for bad_hash in (
            "abc123",
            "g" * 40,
            "A" * 40,
            "a" * 39,
            "a" * 41,
            "a" * 63,
            "a" * 65,
            f"{movie_hash}|{adult_hash}",
            f"{movie_hash}\n",
        ):
            res_bad = await client.post(
                "/api/queue/action",
                json={"hashes": [bad_hash], "action": "pause"},
            )
            assert res_bad.status_code == 400
        control_mock.assert_not_awaited()

        # 3. While vault is locked, hidden adult hash (alone or batched) and unknown hash return 404
        res_adult_locked = await client.post(
            "/api/queue/action",
            json={"hashes": [adult_hash], "action": "pause"},
        )
        assert res_adult_locked.status_code == 404

        res_mixed_locked = await client.post(
            "/api/queue/action",
            json={"hashes": [movie_hash, adult_hash], "action": "pause"},
        )
        assert res_mixed_locked.status_code == 404

        res_missing = await client.post(
            "/api/queue/action",
            json={"hashes": [missing_hash], "action": "pause"},
        )
        assert res_missing.status_code == 404
        control_mock.assert_not_awaited()

        # 4. Visible 40-char movie hash succeeds while vault is locked
        res_movie = await client.post(
            "/api/queue/action",
            json={"hashes": [movie_hash], "action": "pause"},
        )
        assert res_movie.status_code == 200
        assert res_movie.json() == {"success": True, "action": "pause", "count": 1}
        control_mock.assert_awaited_once_with(action="pause", hashes=[movie_hash], delete_files=False)

        # 5. Once vault is unlocked, the 64-char adult hash is visible and succeeds
        control_mock.reset_mock()
        unlock_res = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock_res.status_code == 200

        res_adult_unlocked = await client.post(
            "/api/queue/action",
            json={"hashes": [adult_hash], "action": "resume"},
        )
        assert res_adult_unlocked.status_code == 200
        assert res_adult_unlocked.json() == {"success": True, "action": "resume", "count": 1}
        control_mock.assert_awaited_once_with(action="resume", hashes=[adult_hash], delete_files=False)


@pytest.mark.asyncio
async def test_docs_and_cors_hardening_respect_debug_flag():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        docs_res = await client.get("/docs")
        openapi_res = await client.get("/openapi.json")
        if not settings.DEBUG:
            assert docs_res.status_code == 404
            assert openapi_res.status_code == 404
        else:
            assert docs_res.status_code == 200

        https_preflight = await client.options(
            "/api/health",
            headers={
                "Origin": f"https://media.{settings.DOMAIN}",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert https_preflight.headers.get("access-control-allow-origin") == f"https://media.{settings.DOMAIN}"

        http_preflight = await client.options(
            "/api/health",
            headers={
                "Origin": f"http://media.{settings.DOMAIN}",
                "Access-Control-Request-Method": "GET",
            },
        )
        if not settings.DEBUG:
            assert "access-control-allow-origin" not in http_preflight.headers


