"""Watch-while-downloading: info-hash helpers, piece maths, grab stream option, stream mode."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.database import init_db
from backend.app.main import app
from backend.app.routers import grab as grab_router
from backend.app.routers import stream as stream_router
from backend.app.services.qbittorrent import AddResult
from backend.app.services.torrent_stream import (
    PIECE_HAVE,
    available_end,
    contiguous_fraction,
    file_offset,
    magnet_info_hash,
    pick_stream_file,
    torrent_info_hash,
)

@pytest_asyncio.fixture(autouse=True)
async def _db():
    await init_db()
    stream_router._qb_window_cache.clear()
    yield


INFO = b"d6:lengthi5e4:name3:foo12:piece lengthi16384ee"
TORRENT = b"d8:announce3:abc4:info" + INFO + b"e"


def test_torrent_info_hash_hashes_the_info_dict():
    assert torrent_info_hash(TORRENT) == hashlib.sha1(INFO).hexdigest()
    assert torrent_info_hash(b"not a torrent") is None
    assert torrent_info_hash(b"d8:announce3:abce") is None


def test_magnet_info_hash_hex_and_base32():
    hex_hash = "0123456789abcdef0123456789abcdef01234567"
    assert magnet_info_hash(f"magnet:?xt=urn:btih:{hex_hash.upper()}&dn=x") == hex_hash
    b32 = base64.b32encode(bytes.fromhex(hex_hash)).decode()
    assert magnet_info_hash(f"magnet:?xt=urn:btih:{b32}") == hex_hash
    assert magnet_info_hash("https://example.com/x.torrent") is None


def test_pick_stream_file_prefers_largest_non_sample_video():
    files = [
        {"name": "Movie/Sample/sample.mkv", "size": 50},
        {"name": "Movie/movie.nfo", "size": 1},
        {"name": "Movie/movie.mkv", "size": 900},
        {"name": "Movie/extras.mkv", "size": 100},
    ]
    pos, entry = pick_stream_file(files)
    assert entry["name"] == "Movie/movie.mkv"
    assert file_offset(files, pos) == 51
    assert pick_stream_file([{"name": "a.txt", "size": 5}]) is None


def test_available_end_clamps_to_downloaded_pieces():
    have, miss = PIECE_HAVE, 0
    states = [have, have, miss, have]  # piece size 10
    # Fully inside downloaded pieces: unchanged.
    assert available_end(0, 19, 0, 10, states) == 19
    # Crosses into missing piece 2: stop at byte 19.
    assert available_end(5, 35, 0, 10, states) == 19
    # Starts in a missing piece: nothing to serve yet.
    assert available_end(25, 30, 0, 10, states) is None
    # File offset shifts the piece maths (file starts at torrent byte 5).
    assert available_end(0, 30, 5, 10, states) == 14
    # Downloading (1) counts as missing.
    assert available_end(0, 9, 0, 10, [1]) is None


def test_contiguous_fraction():
    assert contiguous_fraction(40, 0, 10, [2, 2, 0, 2]) == 0.5
    assert contiguous_fraction(40, 0, 10, [0, 2, 2, 2]) == 0.0
    assert contiguous_fraction(40, 0, 10, [2, 2, 2, 2]) == 1.0


async def _login(client: AsyncClient) -> None:
    res = await client.post(
        "/api/auth/login",
        json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
    )
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_quick_grab_stream_option_sets_sequential_and_returns_hash(monkeypatch):
    add_mock = AsyncMock(return_value=AddResult(True, None, 200, "ab" * 20))
    monkeypatch.setattr(grab_router.qb_client, "add_torrent_with_status", add_mock)
    monkeypatch.setattr(grab_router.cross_seed_client, "trigger_search", AsyncMock(return_value=True))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        res = await client.post(
            "/api/grab/quick",
            json={
                "title": "Some.Movie.2026.1080p.WEB-DL",
                "magnet_url": "magnet:?xt=urn:btih:" + ("ab" * 20),
                "category": "Movies",
                "stream": True,
            },
        )
        assert res.status_code == 200
        assert res.json()["info_hash"] == "ab" * 20
        assert add_mock.await_args.kwargs["sequential"] is True

        add_mock.reset_mock()
        res = await client.post(
            "/api/grab/quick",
            json={"title": "Other.2026", "magnet_url": "magnet:?xt=urn:btih:" + ("cd" * 20), "category": "Movies"},
        )
        assert res.status_code == 200
        assert add_mock.await_args.kwargs["sequential"] is False


@pytest.mark.asyncio
async def test_stream_mode_hides_adult_torrents_while_locked(monkeypatch):
    adult_hash = "ef" * 20
    monkeypatch.setattr(
        "backend.app.services.streaming.qb_client.get_queue",
        AsyncMock(return_value=[{"hash": adult_hash, "name": "Adult.Release", "category": "adult", "content_path": "/x"}]),
    )
    enable = AsyncMock(return_value={"seq_dl": True, "f_l_piece_prio": True, "progress": 0.1})
    monkeypatch.setattr(stream_router.qb_client, "enable_stream_mode", enable)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        locked = await client.post(f"/api/stream/qb/{adult_hash}/stream-mode")
        assert locked.status_code == 404
        enable.assert_not_awaited()

        assert (await client.post("/api/vault/unlock", json={"pin": settings.VAULT_PIN})).status_code == 200
        unlocked = await client.post(f"/api/stream/qb/{adult_hash}/stream-mode")
        assert unlocked.status_code == 200
        assert unlocked.json() == {"hash": adult_hash, "streaming": True, "progress": 10.0}


@pytest.mark.asyncio
async def test_clamp_waits_then_serves_only_downloaded_bytes(monkeypatch):
    windows = [
        {"path": "/srv/t/m.mkv", "size": 40, "complete": False, "offset": 0, "piece_size": 10,
         "states": [2, 0, 0, 0], "progress": 0.25, "streaming": True, "playable": 0.25},
    ]

    async def fake_window(qb_hash, fresh=False):
        return windows[0]

    monkeypatch.setattr(stream_router, "_qb_stream_window", fake_window)
    path, rng = await stream_router._clamp_to_downloaded("ab" * 20, "bytes=0-")
    assert (path, rng) == ("/srv/t/m.mkv", "bytes=0-9")

    monkeypatch.setattr(stream_router, "_QB_WAIT_SECONDS", 0.0)
    with pytest.raises(Exception) as exc:
        await stream_router._clamp_to_downloaded("ab" * 20, "bytes=15-20")
    assert getattr(exc.value, "status_code", None) == 503
