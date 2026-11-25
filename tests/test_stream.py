import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app
from backend.app.services.library_mirror import replace_service_library_items
from backend.app.services.streaming import (
    build_jellyfin_hls_url,
    create_stream_token,
    parse_range_header,
    verify_stream_token,
)


@pytest_asyncio.fixture(autouse=True)
async def setup_stream_db():
    await init_db()


def test_hmac_stream_token_signing_tamper_and_expiry():
    tok = create_stream_token({"item_id": "radarr:7", "is_adult": False}, ttl_seconds=60)
    payload = verify_stream_token(tok)
    assert payload is not None
    assert payload["item_id"] == "radarr:7"

    # Tampered signature fails
    raw, sig = tok.split(".", 1)
    bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert verify_stream_token(f"{raw}.{bad_sig}") is None

    # Expired token fails
    expired = create_stream_token({"item_id": "radarr:7"}, ttl_seconds=-10)
    assert verify_stream_token(expired) is None


def test_http_206_range_header_math_and_jellyfin_hls_url():
    assert parse_range_header(None, 100) == (0, 99)
    assert parse_range_header("bytes=0-9", 100) == (0, 9)
    assert parse_range_header("bytes=50-", 100) == (50, 99)
    assert parse_range_header("bytes=-10", 100) == (90, 99)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_range_header("bytes=200-250", 100)
    assert exc.value.status_code == 416

    hls = build_jellyfin_hls_url("abc123", base_url="https://seedbox.local/jellyfin", api_key="jfkey")
    assert "/Videos/abc123/master.m3u8" in hls
    assert "VideoCodec=hevc,h264" in hls
    assert "AllowVideoStreamCopy=true" in hls
    assert "AudioCodec=aac" in hls
    assert "TranscodingMaxAudioChannels=2" in hls
    assert "MaxStreamingBitrate=120000000" in hls


@pytest.mark.asyncio
async def test_stream_manifest_vault_404_stealth_and_206_file_stream(tmp_path: Path):
    await replace_service_library_items(
        "radarr",
        [{
            "id": 101,
            "title": "Dune Part Two 2160p DV REMUX",
            "year": 2024,
            "has_file": True,
            "file_path": "/home/seedbox/media/Movies/Dune.Part.Two.2024.2160p.DV.REMUX.mkv",
        }],
    )
    await replace_service_library_items(
        "whisparr",
        [{
            "id": 202,
            "title": "Adult Studio Release 2160p",
            "year": 2025,
            "has_file": True,
            "file_path": "/home/seedbox/media/adult/Adult.Studio.Release.2160p.mp4",
        }],
    )

    sample_file = tmp_path / "sample.mp4"
    sample_file.write_bytes(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD})
        assert login.status_code == 200

        # 1. Non-adult manifest returns HLS 4K copy params + direct HMAC URL + vlc/mpv links
        res_movie = await client.get("/api/stream/radarr:101/manifest")
        assert res_movie.status_code == 200
        m_data = res_movie.json()
        assert m_data["resolution"] == "4K 2160p"
        assert m_data["hdr_format"] == "Dolby Vision"
        assert m_data["dv_profile_notice"] is True
        assert "AllowVideoStreamCopy=true" in m_data["hls_url"]
        assert "MaxStreamingBitrate=120000000" in m_data["hls_url"]
        assert m_data["direct_url"].startswith("/api/stream/file/")
        assert m_data["vlc_url"].startswith("vlc://")
        assert m_data["mpv_command"].startswith('mpv "')

        # 2. Adult item returns 404 when vault locked
        locked_adult = await client.get("/api/stream/whisparr:202/manifest")
        assert locked_adult.status_code == 404

        # Unlock vault -> adult manifest succeeds
        unlock = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock.status_code == 200
        unlocked_adult = await client.get("/api/stream/whisparr:202/manifest")
        assert unlocked_adult.status_code == 200
        adult_direct_url = unlocked_adult.json()["direct_url"]

        # Re-lock vault -> previously issued adult direct URL must return 404 stealth
        await client.post("/api/vault/lock")
        adult_stream_locked = await client.get(adult_direct_url)
        assert adult_stream_locked.status_code == 404

        # 3. Byte-range streaming returns 206 Partial Content with exact slice and explicit MIME
        mkv_file = tmp_path / "sample.mkv"
        mkv_file.write_bytes(b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        token_local = create_stream_token({"item_id": "qb:abc", "is_adult": False, "file_path": str(sample_file)})
        token_mkv = create_stream_token({"item_id": "qb:mkv", "is_adult": False, "file_path": str(mkv_file)})
        with patch.object(settings, "STREAM_MEDIA_ROOT", str(tmp_path)):
            range_res = await client.get(f"/api/stream/file/{token_local}", headers={"Range": "bytes=4-11"})
            assert range_res.status_code == 206
            assert range_res.headers["accept-ranges"] == "bytes"
            assert range_res.headers["content-range"] == f"bytes 4-11/{sample_file.stat().st_size}"
            assert range_res.headers["content-type"].startswith("video/mp4")
            assert range_res.content == b"456789AB"

            mkv_res = await client.get(f"/api/stream/file/{token_mkv}", headers={"Range": "bytes=0-3"})
            assert mkv_res.status_code == 206
            assert mkv_res.headers["content-type"].startswith("video/x-matroska")
            assert mkv_res.content == b"0123"


@pytest.mark.asyncio
async def test_stream_hls_proxy_and_connect_error_returns_structured_502(monkeypatch):
    from unittest.mock import MagicMock
    import httpx

    monkeypatch.setattr(settings, "JELLYFIN_URL", "https://seedbox.local/jellyfin")
    monkeypatch.setattr(settings, "JELLYFIN_API_KEY", "test-jf-key")

    # 1. HLS proxy fetches master.m3u8 with MediaBrowser Token header and rewrites absolute paths
    fake_hls_res = MagicMock()
    fake_hls_res.status_code = 200
    fake_hls_res.headers = {"content-type": "application/vnd.apple.mpegurl"}
    fake_hls_res.aread = AsyncMock(
        return_value=b"#EXTM3U\nhttps://seedbox.local/jellyfin/Videos/jf999/main.m3u8?MediaSourceId=jf999\n"
    )
    fake_hls_res.aclose = AsyncMock()

    fake_client = MagicMock()
    fake_client.build_request = MagicMock(return_value=MagicMock())
    fake_client.send = AsyncMock(return_value=fake_hls_res)
    monkeypatch.setattr("backend.app.routers.stream.get_search_client", lambda: fake_client)

    tok = create_stream_token({"item_id": "radarr:101", "is_adult": False, "jf_id": "jf999"})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        hls_ok = await client.get(
            f"/api/stream/hls/{tok}/master.m3u8?VideoCodec=hevc,h264&AllowVideoStreamCopy=true&AudioCodec=aac"
        )
        assert hls_ok.status_code == 200
        assert f"/api/stream/hls/{tok}/main.m3u8" in hls_ok.text
        built_headers = fake_client.build_request.call_args.kwargs["headers"]
        assert built_headers["Authorization"] == 'MediaBrowser Token="test-jf-key"'

        # 2. Upstream ConnectError in /api/stream/file/{token} & /hls returns structured 502 (never unhandled 500)
        fake_client.send = AsyncMock(side_effect=httpx.ConnectError("All connection attempts failed"))
        err_file = await client.get(f"/api/stream/file/{tok}")
        assert err_file.status_code == 502
        assert "ConnectError" in err_file.json()["detail"]

        err_hls = await client.get(f"/api/stream/hls/{tok}/master.m3u8")
        assert err_hls.status_code == 502
        assert "ConnectError" in err_hls.json()["detail"]


@pytest.mark.asyncio
async def test_stream_file_sftp_206_range_and_mime(monkeypatch):
    async def _fake_sftp_open(raw_path: str, range_hdr: str | None):
        if raw_path.endswith(".mp4"):
            async def _gen_mp4():
                yield b"MP4DATA"

            return 206, {"Accept-Ranges": "bytes", "Content-Length": "7", "Content-Range": "bytes 0-6/20"}, "video/mp4", _gen_mp4()

        assert raw_path == "/home/seedbox/torrents/movie.mkv"
        assert range_hdr == "bytes=2-5"

        async def _gen():
            yield b"CDEF"

        return 206, {"Accept-Ranges": "bytes", "Content-Length": "4", "Content-Range": "bytes 2-5/10"}, "video/x-matroska", _gen()

    monkeypatch.setattr("backend.app.routers.stream.open_sftp_range_stream", _fake_sftp_open)
    tok = create_stream_token(
        {"item_id": "qb:sftp1", "is_adult": False, "file_path": "/home/seedbox/torrents/movie.mkv"}
    )
    tok_mp4 = create_stream_token(
        {"item_id": "whisparr:202", "is_adult": False, "file_path": "/home/seedbox/media/adult/scene.mp4"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(f"/api/stream/file/{tok}", headers={"Range": "bytes=2-5"})
        assert res.status_code == 206
        assert res.headers["content-type"].startswith("video/x-matroska")
        assert res.headers["content-range"] == "bytes 2-5/10"
        assert res.content == b"CDEF"

        res_mp4 = await client.get(f"/api/stream/file/{tok_mp4}", headers={"Range": "bytes=0-6"})
        assert res_mp4.status_code == 206
        assert res_mp4.headers["content-type"].startswith("video/mp4")
        assert res_mp4.content == b"MP4DATA"


@pytest.mark.asyncio
async def test_whisparr_on_demand_moviefile_resolution_and_manifest_404(monkeypatch):
    from unittest.mock import MagicMock
    from sqlalchemy import text
    from backend.app.database import engine
    from backend.app.services.arr_client import radarr_client, whisparr_client

    # Store whisparr:202 with a directory-only path (no video extension) and radarr:999 with empty file_path
    await replace_service_library_items(
        "whisparr",
        [{
            "id": 202,
            "title": "Whisparr Scene 2160p",
            "year": 2026,
            "has_file": True,
            "file_path": "/home/seedbox/media/adult/Whisparr Scene",
        }],
    )
    await replace_service_library_items(
        "radarr",
        [{"id": 999, "title": "Unresolved Missing Movie", "year": 2026, "has_file": False, "file_path": ""}],
    )

    real_mp4 = "/home/seedbox/media/adult/Whisparr Scene/scene.mp4"
    monkeypatch.setattr(whisparr_client, "api_key", "wp-test-key")
    monkeypatch.setattr(radarr_client, "api_key", "rad-test-key")
    monkeypatch.setattr("backend.app.routers.stream.resolve_jellyfin_id", AsyncMock(return_value=("fallback", False)))

    async def _fake_get(url: str, **kwargs):
        if "/api/v3/movie/202" in url:
            return MagicMock(status_code=200, json=lambda: {"id": 202, "movieFile": {"path": real_mp4}})
        return MagicMock(status_code=404, json=lambda: {})

    fake_http = MagicMock()
    fake_http.get = AsyncMock(side_effect=_fake_get)
    monkeypatch.setattr("backend.app.services.arr_client.get_search_client", lambda: fake_http)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/api/auth/login", json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD})
        assert login.status_code == 200
        unlock = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock.status_code == 200

        # 1. whisparr:202 resolves movieFile.path on demand, persists to SQLite, and signs real token
        res = await client.get("/api/stream/whisparr:202/manifest")
        assert res.status_code == 200
        tok = res.json()["direct_url"].rsplit("/", 1)[-1]
        assert verify_stream_token(tok)["file_path"] == real_mp4

        async with engine.connect() as conn:
            persisted = (await conn.execute(
                text("SELECT file_path FROM library_items WHERE service='whisparr' AND external_id=202")
            )).scalar()
        assert persisted == real_mp4

        # 2. radarr:999 with neither file_path nor jf_id returns 404 from /manifest
        missing_res = await client.get("/api/stream/radarr:999/manifest")
        assert missing_res.status_code == 404
        assert "Media file path not available" in missing_res.json()["detail"]


@pytest.mark.asyncio
async def test_playback_progress_sync_vault_stealth_handoffs_and_hero_reel(tmp_path: Path):
    await replace_service_library_items(
        "radarr",
        [{
            "id": 303,
            "title": "Heat 1995 2160p REMUX",
            "year": 1995,
            "has_file": True,
            "file_path": "/home/seedbox/media/Movies/Heat.1995.2160p.REMUX.mkv",
        }],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Unauthenticated playback access fails with 401
        unauth = await client.get("/api/playback/progress")
        assert unauth.status_code == 401

        login = await client.post("/api/auth/login", json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD})
        assert login.status_code == 200

        # 1. Save progress for radarr:303 and verify calculation
        save_res = await client.post(
            "/api/playback/progress",
            json={
                "item_id": "radarr:303",
                "title": "Heat 1995 2160p REMUX",
                "position_seconds": 1245.5,
                "duration_seconds": 10200.0,
            },
        )
        assert save_res.status_code == 200
        body = save_res.json()
        assert body["item_id"] == "radarr:303"
        assert body["position_seconds"] == pytest.approx(1245.5, abs=0.1)
        assert body["completed"] is False

        # 2. GET item progress returns saved position
        get_item = await client.get("/api/playback/progress?item_id=radarr:303")
        assert get_item.status_code == 200
        assert get_item.json()["position_seconds"] == pytest.approx(1245.5, abs=0.1)

        # 3. Manifest includes resume_seconds and HTTPS-forwarded mpv://, infuse://, vlc:// links
        manifest = await client.get(
            "/api/stream/radarr:303/manifest",
            headers={"X-Forwarded-Proto": "https", "Host": "media.example.com"},
        )
        assert manifest.status_code == 200
        m = manifest.json()
        assert m["resume_seconds"] == pytest.approx(1245.5, abs=0.1)
        assert m["mpv_url"].startswith("mpv://https://media.example.com/api/stream/file/")
        assert m["vlc_url"].startswith("vlc://https://media.example.com/api/stream/file/")
        assert m["infuse_url"].startswith("infuse://x-callback-url/play?url=https%3A%2F%2Fmedia.example.com%2Fapi%2Fstream%2Ffile%2F")

        # 4. Vault stealth 404 isolation for whisparr:* playback progress
        locked_post = await client.post(
            "/api/playback/progress",
            json={"item_id": "whisparr:202", "title": "Secret", "position_seconds": 60, "duration_seconds": 600},
        )
        assert locked_post.status_code == 404

        unlock = await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})
        assert unlock.status_code == 200
        unlocked_post = await client.post(
            "/api/playback/progress",
            json={"item_id": "whisparr:202", "title": "Secret", "position_seconds": 60, "duration_seconds": 600},
        )
        assert unlocked_post.status_code == 200

        list_unlocked = await client.get("/api/playback/progress")
        assert any(it["item_id"] == "whisparr:202" for it in list_unlocked.json()["items"])

        await client.post("/api/vault/lock")
        locked_get = await client.get("/api/playback/progress?item_id=whisparr:202")
        assert locked_get.status_code == 404
        list_locked = await client.get("/api/playback/progress")
        assert all(not it["item_id"].startswith("whisparr:") for it in list_locked.json()["items"])

        # 5. Hero reel & runtime DATA_DIR override
        reel_res = await client.get("/media/hero-reel.mp4")
        assert reel_res.status_code == 200
        assert reel_res.headers["content-type"] == "video/mp4"
        assert len(reel_res.content) < 1_800_000

        override_dir = Path(settings.DATA_DIR) / "media"
        override_dir.mkdir(parents=True, exist_ok=True)
        override_file = override_dir / "hero-reel.mp4"
        try:
            override_file.write_bytes(b"CUSTOM-REEL-BYTES")
            custom_res = await client.get("/media/hero-reel.mp4")
            assert custom_res.status_code == 200
            assert custom_res.content == b"CUSTOM-REEL-BYTES"
        finally:
            override_file.unlink(missing_ok=True)

