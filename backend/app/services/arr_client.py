import asyncio
import logging
import re
from typing import Optional, List, Dict, Any, Tuple
from backend.app.config import settings
from backend.app.services.http_clients import get_search_client, get_telemetry_client
from backend.app.services.url_safety import (
    has_control_chars,
    is_valid_magnet_uri,
    validate_download_url,
)

logger = logging.getLogger(__name__)

_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_EPISODE_TAIL_RE = re.compile(
    r"(?:^|\s)(?:"
    r"s\d{1,2}(?:e\d{1,3}(?:[-e]\d{1,3})*)?"
    r"|\d{1,2}x\d{2}"
    r"|season\s+\d+"
    r"|complete\s+series"
    r"|(?:19|20)\d{2}\s+\d{2}\s+\d{2}"
    r")\b.*$",
    re.IGNORECASE,
)
_RELEASE_TAIL_RE = re.compile(
    r"(?:^|\s)(?:"
    r"2160p|1080p|720p|480p|576p|4k|uhd|fhd|remux|blu-?ray|bdrip|brrip|"
    r"web-?dl|webdl|webrip|hdtv|dvdrip|hdrip|hdcam|camrip|telesync|"
    r"x264|x265|h\.?\s*264|h\.?\s*265|hevc|avc|av1|xvid|10bit|"
    r"hdr10?\+?|dovi|dolby\s+vision|atmos|truehd|dts-?hd|dtshd|ddp?\d(?:\s*\d)?|aac|ac3|eac3|flac|"
    r"remastered|proper|repack|internal|multi|dual|mp4|mkv|avi"
    r")\b.*$",
    re.IGNORECASE,
)
_PAREN_YEAR_RE = re.compile(r"\s*\(((?:19|20)\d{2})\)")
_TRAILING_YEAR_RE = re.compile(r"\s+((?:19|20)\d{2})$")


def _clean_release_title(title: str) -> Tuple[str, Optional[int]]:
    """Extract a clean media title (and optional trailing year) from a scene release name."""
    cleaned = _BRACKET_TAG_RE.sub(" ", title or "")
    cleaned = _PAREN_YEAR_RE.sub(r" \1", cleaned)
    cleaned = re.sub(r"[._]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    cleaned = _EPISODE_TAIL_RE.sub("", cleaned).strip()
    cleaned = _RELEASE_TAIL_RE.sub("", cleaned).strip()
    cleaned = re.sub(r"[\s\-]+$", "", cleaned).strip()

    extracted_year: Optional[int] = None
    year_match = _TRAILING_YEAR_RE.search(cleaned)
    if year_match:
        extracted_year = int(year_match.group(1))

    return cleaned, extracted_year


def _build_lookup_candidates(
    title: str,
    is_sonarr: bool = False,
    tmdb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
) -> Tuple[List[str], Optional[int]]:
    """Return ordered, deduplicated *arr lookup terms and any extracted release year."""
    candidates: List[str] = []
    if is_sonarr and tvdb_id:
        candidates.append(f"tvdb:{int(tvdb_id)}")
    elif not is_sonarr and tmdb_id:
        candidates.append(f"tmdb:{int(tmdb_id)}")

    cleaned, extracted_year = _clean_release_title(title)
    if cleaned:
        candidates.append(cleaned)
        no_year = _TRAILING_YEAR_RE.sub("", cleaned).strip()
        if no_year and no_year != cleaned:
            candidates.append(no_year)

    if title and title not in candidates:
        candidates.append(title)

    return list(dict.fromkeys(candidates)), extracted_year


class ArrClient:
    def __init__(
        self,
        service_name: str,
        base_url: str = "",
        api_key: str = "",
        root_folder: str = "",
        *,
        settings_prefix: Optional[str] = None,
    ):
        self.name = service_name
        self._settings_prefix = settings_prefix
        self._static_base_url = base_url.rstrip("/")
        self._static_api_key = api_key
        self._static_root_folder = root_folder

    @property
    def base_url(self) -> str:
        if self._settings_prefix:
            return str(getattr(settings, f"{self._settings_prefix}_URL", "") or "").rstrip("/")
        return self._static_base_url

    @base_url.setter
    def base_url(self, value: str) -> None:
        if self._settings_prefix:
            setattr(settings, f"{self._settings_prefix}_URL", value)
        else:
            self._static_base_url = value.rstrip("/")

    @property
    def api_key(self) -> str:
        if self._settings_prefix:
            return str(getattr(settings, f"{self._settings_prefix}_API_KEY", "") or "")
        return self._static_api_key

    @api_key.setter
    def api_key(self, value: str) -> None:
        if self._settings_prefix:
            setattr(settings, f"{self._settings_prefix}_API_KEY", value)
        else:
            self._static_api_key = value

    @property
    def root_folder(self) -> str:
        if self._settings_prefix:
            return str(getattr(settings, f"{self._settings_prefix}_ROOT_FOLDER", "") or "")
        return self._static_root_folder

    @root_folder.setter
    def root_folder(self, value: str) -> None:
        if self._settings_prefix:
            setattr(settings, f"{self._settings_prefix}_ROOT_FOLDER", value)
        else:
            self._static_root_folder = value

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def check_health(self) -> Tuple[bool, Optional[str], Optional[int]]:
        if not self.base_url or not self.api_key:
            return False, "Not Configured", None
        try:
            client = get_telemetry_client()
            res = await client.get(
                self._url("/api/v3/system/status"),
                headers=self._get_headers(),
                timeout=1.5,
            )
            if res.status_code == 200:
                data = res.json()
                version = data.get("version", "Connected")
                return True, version, 200
            return False, None, res.status_code
        except Exception:
            return False, None, None

    async def _fetch_series_episode_file_path(self, client: Any, series_id: Any) -> str:
        """Return the first available episodeFile.path for a Sonarr/Whisparr-v2 series."""
        try:
            res = await client.get(
                self._url("/api/v3/episodefile"),
                headers=self._get_headers(),
                params={"seriesId": series_id},
                timeout=3.0,
            )
            if res.status_code == 200:
                files = res.json() or []
                for f in files:
                    if isinstance(f, dict) and f.get("path"):
                        return str(f["path"])
        except Exception:
            pass
        return ""

    async def fetch_library_titles_with_status(self) -> Tuple[bool, List[Dict[str, Any]]]:
        """Returns (reachable_ok, library_items) without masking offline errors as empty libraries."""
        if not self.api_key or not self.base_url:
            return False, []
        is_sonarr = self.name.lower() == "sonarr"
        endpoints = ["/api/v3/series"] if is_sonarr else ["/api/v3/movie", "/api/v3/series"]
        try:
            client = get_search_client()
            res = None
            used_series = False
            for ep in endpoints:
                res = await client.get(self._url(ep), headers=self._get_headers(), timeout=5.0)
                if res.status_code == 200:
                    used_series = ep.endswith("/series")
                    break
                if res.status_code != 404:
                    break
            if res is not None and res.status_code == 200:
                items = res.json() or []
                series_path_map: Dict[Any, str] = {}
                if used_series:
                    series_ids_needed = [
                        it.get("id")
                        for it in items
                        if int((it.get("statistics") or {}).get("episodeFileCount") or 0) > 0
                        and it.get("id") is not None
                    ]
                    if series_ids_needed:
                        sem = asyncio.Semaphore(6)

                        async def _get_ep_path(series_id: Any) -> str:
                            async with sem:
                                return await self._fetch_series_episode_file_path(client, series_id)

                        fetched_paths = await asyncio.gather(
                            *(_get_ep_path(sid) for sid in series_ids_needed),
                            return_exceptions=True,
                        )
                        series_path_map = {
                            sid: (p if isinstance(p, str) else "")
                            for sid, p in zip(series_ids_needed, fetched_paths)
                        }

                results = []
                for it in items:
                    if used_series:
                        stats = it.get("statistics") or {}
                        ep_count = int(stats.get("episodeFileCount") or 0)
                        file_path = series_path_map.get(it.get("id"), "") if ep_count > 0 else ""
                        has_file = bool(ep_count > 0 or file_path)
                        size_bytes = int(stats.get("sizeOnDisk") or it.get("sizeOnDisk") or 0)
                    else:
                        movie_file = it.get("movieFile") or {}
                        file_path = str(movie_file.get("path") or "")
                        has_file = bool(it.get("hasFile") or file_path)
                        size_bytes = int(it.get("sizeOnDisk") or movie_file.get("size") or 0)
                    results.append({
                        "title": it.get("title", ""),
                        "clean_title": it.get("cleanTitle", ""),
                        "year": it.get("year", 0),
                        "id": it.get("id"),
                        "has_file": has_file,
                        "monitored": it.get("monitored", True),
                        "file_path": file_path,
                        "size_bytes": size_bytes,
                    })
                return True, results
        except Exception:
            pass
        return False, []

    async def fetch_item_file_path(self, external_id: int) -> str:
        """Fetch the concrete media file path for a Radarr/Whisparr/Sonarr item on demand."""
        if not self.api_key or not self.base_url:
            return ""
        client = get_search_client()
        svc = self.name.lower()
        if svc != "sonarr":
            try:
                res = await client.get(
                    self._url(f"/api/v3/movie/{external_id}"),
                    headers=self._get_headers(),
                    timeout=4.0,
                )
                if res.status_code == 200:
                    data = res.json() or {}
                    fp = str((data.get("movieFile") or {}).get("path") or "")
                    if fp:
                        return fp
            except Exception:
                pass
        # Sonarr or Whisparr v2 series/episodefile mode
        try:
            res_ep = await client.get(
                self._url(f"/api/v3/episodefile/{external_id}"),
                headers=self._get_headers(),
                timeout=4.0,
            )
            if res_ep.status_code == 200:
                fp = str((res_ep.json() or {}).get("path") or "")
                if fp:
                    return fp
        except Exception:
            pass
        return await self._fetch_series_episode_file_path(client, external_id)

    async def get_library_titles(self) -> List[Dict[str, Any]]:
        """Returns existing library items (title, year, id, hasFile)."""
        _, items = await self.fetch_library_titles_with_status()
        return items

    async def lookup(self, term: str) -> List[Dict[str, Any]]:
        """Performs metadata lookup for title."""
        endpoint = "/api/v3/series/lookup" if self.name.lower() == "sonarr" else "/api/v3/movie/lookup"
        try:
            client = get_search_client()
            res = await client.get(
                self._url(endpoint),
                headers=self._get_headers(),
                params={"term": term},
                timeout=6.0,
            )
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass
        return []

    async def add_and_monitor(
        self,
        title: str,
        year: Optional[int] = None,
        tmdb_id: Optional[int] = None,
        tvdb_id: Optional[int] = None,
        download_url: Optional[str] = None,
        protocol: str = "torrent",
    ) -> Tuple[bool, str]:
        """Adds and monitors title in *arr, triggering search or release grab."""
        if not self.api_key:
            return False, f"{self.name} API key not configured"

        is_sonarr = self.name.lower() == "sonarr"
        candidates, extracted_year = _build_lookup_candidates(
            title,
            is_sonarr=is_sonarr,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
        )

        # Lookup cleaned title (or external ID) first to obtain canonical metadata
        lookup_results: List[Dict[str, Any]] = []
        for term in candidates:
            lookup_results = await self.lookup(term)
            if lookup_results:
                break
        if not lookup_results:
            return False, f"Could not find metadata for '{title}' in {self.name}"

        expected_year = year or extracted_year
        item = next(
            (r for r in lookup_results if expected_year and r.get("year") == expected_year),
            lookup_results[0],
        )
        # Check if already added
        if item.get("id", 0) > 0 and item.get("monitored", False):
            # Already in library, push release if download_url available
            if download_url:
                await self.push_release(title, download_url, protocol)
                return True, f"Already in {self.name}; pushed release to download queue."
            return True, f"'{title}' is already in {self.name} library."

        endpoint = "/api/v3/series" if is_sonarr else "/api/v3/movie"

        payload: Dict[str, Any] = {
            "title": item.get("title", title),
            "qualityProfileId": 1,
            "rootFolderPath": self.root_folder,
            "monitored": True,
        }

        if is_sonarr:
            payload.update({
                "tvdbId": tvdb_id or item.get("tvdbId"),
                "titleSlug": item.get("titleSlug"),
                "images": item.get("images", []),
                "seasons": item.get("seasons", []),
                "addOptions": {"searchForMissingEpisodes": True},
            })
        else:
            payload.update({
                "tmdbId": tmdb_id or item.get("tmdbId"),
                "year": year or item.get("year"),
                "titleSlug": item.get("titleSlug"),
                "images": item.get("images", []),
                "addOptions": {"searchForMovie": True},
            })

        try:
            client = get_search_client()
            res = await client.post(
                self._url(endpoint),
                headers=self._get_headers(),
                json=payload,
                timeout=8.0,
            )
            if res.status_code in (200, 201):
                # Also push specific release if URL provided
                if download_url:
                    await self.push_release(title, download_url, protocol)
                return True, f"Successfully added and monitored in {self.name} (Destination: {self.root_folder})"
            error_detail = res.text[:200]
            return False, f"Failed adding to {self.name}: {error_detail}"
        except Exception as e:
            return False, f"Error communicating with {self.name}: {str(e)}"

    async def get_queue(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """Fetch active *arr queue records including tracked download status messages."""
        if not self.api_key or not self.base_url:
            return []
        try:
            client = get_telemetry_client()
            res = await client.get(
                self._url("/api/v3/queue"),
                headers=self._get_headers(),
                params={
                    "page": 1,
                    "pageSize": int(page_size),
                    "includeUnknownMovieItems": "true",
                    "includeUnknownSeriesItems": "true",
                },
                timeout=4.0,
            )
            if res.status_code == 200:
                payload = res.json()
                return list(payload.get("records") or []) if isinstance(payload, dict) else list(payload or [])
        except Exception:
            pass
        return []

    async def delete_queue_item(
        self,
        queue_id: int,
        remove_from_client: bool = True,
        blocklist: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """Remove a stalled/failed queue entry and optionally blocklist its release."""
        if not self.api_key or not self.base_url:
            return False, f"{self.name} API key not configured"
        try:
            client = get_search_client()
            res = await client.delete(
                self._url(f"/api/v3/queue/{int(queue_id)}"),
                headers=self._get_headers(),
                params={
                    "removeFromClient": str(bool(remove_from_client)).lower(),
                    "blocklist": str(bool(blocklist)).lower(),
                },
                timeout=6.0,
            )
            if res.status_code in (200, 202, 204):
                return True, None
            return False, f"{self.name} queue delete returned HTTP {res.status_code}"
        except Exception as exc:
            return False, f"{self.name} queue delete failed: {exc}"

    async def trigger_search(
        self,
        media_id: Optional[int] = None,
        episode_ids: Optional[List[int]] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Trigger a replacement *arr search command after blacklisting a bad queue entry."""
        if not self.api_key or not self.base_url:
            return False, f"{self.name} API key not configured"
        svc = self.name.lower()
        if svc == "sonarr":
            if episode_ids:
                cmd: Dict[str, Any] = {"name": "EpisodeSearch", "episodeIds": [int(e) for e in episode_ids]}
            elif media_id:
                cmd = {"name": "SeriesSearch", "seriesId": int(media_id)}
            else:
                cmd = {"name": "MissingEpisodeSearch"}
        else:
            if media_id:
                cmd = {"name": "MoviesSearch", "movieIds": [int(media_id)]}
            else:
                cmd = {"name": "MissingMoviesSearch"}
        try:
            client = get_search_client()
            res = await client.post(
                self._url("/api/v3/command"),
                headers=self._get_headers(),
                json=cmd,
                timeout=6.0,
            )
            if res.status_code in (200, 201, 202):
                return True, None
            return False, f"{self.name} command returned HTTP {res.status_code}"
        except Exception as exc:
            return False, f"{self.name} search command failed: {exc}"

    async def push_release(self, title: str, download_url: str, protocol: str = "torrent") -> bool:
        """Pushes release to *arr /api/v3/release/push to trigger download client and hardlinks."""
        clean_url = (download_url or "").strip()
        if not clean_url or has_control_chars(clean_url):
            return False
        if not is_valid_magnet_uri(clean_url):
            ok_url, reason = await validate_download_url(clean_url, allow_prowlarr=True)
            if not ok_url:
                logger.warning("Blocked unsafe download_url before %s release/push: %s", self.name, reason)
                return False
        try:
            client = get_search_client()
            res = await client.post(
                self._url("/api/v3/release/push"),
                headers=self._get_headers(),
                json={
                    "title": title,
                    "downloadUrl": clean_url,
                    "protocol": protocol,
                    "publishDate": "2026-01-01T00:00:00Z",
                },
                timeout=6.0,
            )
            return res.status_code in (200, 201)
        except Exception:
            return False


radarr_client = ArrClient("Radarr", settings_prefix="RADARR")
sonarr_client = ArrClient("Sonarr", settings_prefix="SONARR")
whisparr_client = ArrClient("Whisparr", settings_prefix="WHISPARR")

