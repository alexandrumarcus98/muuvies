"""Jellyfin lookup must not fall back to a different episode or film."""

from __future__ import annotations

import pytest

from backend.app.services import streaming


class _Res:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"Items": self._items}


class _Client:
    def __init__(self, items):
        self._items = items

    async def get(self, *args, **kwargs):
        return _Res(self._items)


@pytest.fixture
def jellyfin(monkeypatch):
    monkeypatch.setattr(streaming.settings, "JELLYFIN_API_KEY", "k")
    monkeypatch.setattr(streaming.settings, "JELLYFIN_URL", "http://jf")

    def use(items):
        monkeypatch.setattr(streaming, "get_telemetry_client", lambda: _Client(items))

    return use


@pytest.mark.asyncio
async def test_episode_lookup_requires_same_season_and_episode(jellyfin):
    jellyfin([
        {"Id": "e1", "Type": "Episode", "ParentIndexNumber": 2, "IndexNumber": 1, "Path": "/x/a.mkv"},
        {"Id": "e4", "Type": "Episode", "ParentIndexNumber": 2, "IndexNumber": 4, "Path": "/x/b.mkv"},
    ])
    assert await streaming.resolve_jellyfin_id("Dexter Resurrection S02E04 Young Blood 1080p", "fb") == ("e4", True)
    assert await streaming.resolve_jellyfin_id("Dexter Resurrection S02E09 1080p", "fb") == ("fb", False)


@pytest.mark.asyncio
async def test_movie_lookup_checks_year_and_prefers_exact_path(jellyfin):
    jellyfin([
        {"Id": "old", "Type": "Movie", "ProductionYear": 1984, "Path": "/m/Dune (1984)/dune.mkv"},
        {"Id": "new", "Type": "Movie", "ProductionYear": 2021, "Path": "/m/Dune (2021)/dune.2021.mkv"},
    ])
    assert await streaming.resolve_jellyfin_id("Dune 2021 2160p", "fb") == ("new", True)
    assert await streaming.resolve_jellyfin_id("Dune 1999 1080p", "fb") == ("fb", False)
    assert await streaming.resolve_jellyfin_id("Dune", "fb", file_path="/m/Dune (1984)/dune.mkv") == ("old", True)
