import logging
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urljoin, quote

import httpx

from backend.app.config import settings
from backend.app.services.http_clients import get_search_client, get_telemetry_client
from backend.app.services.torrent_stream import magnet_info_hash, normalize_info_hash, torrent_info_hash
from backend.app.services.url_safety import (
    MAX_REDIRECT_HOPS,
    MAX_TORRENT_BYTES,
    has_control_chars,
    is_prowlarr_origin,
    is_valid_magnet_uri,
    validate_download_url,
)

logger = logging.getLogger(__name__)


class AddResult(tuple):
    """(ok, error_detail, http_status) that also carries the added torrent's info-hash.

    Still unpacks as a 3-tuple, so existing callers and test doubles keep working.
    """

    info_hash: Optional[str]

    def __new__(cls, ok: bool, error: Optional[str], status: int, info_hash: Optional[str] = None):
        obj = super().__new__(cls, (ok, error, status))
        obj.info_hash = info_hash
        return obj


def format_bytes(bytes_num: int) -> str:
    if bytes_num <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    val = float(bytes_num)
    while val >= 1024.0 and i < len(units) - 1:
        val /= 1024.0
        i += 1
    return f"{val:.2f} {units[i]}"


def format_speed(bps: int) -> str:
    return f"{format_bytes(bps)}/s"


def format_eta(seconds: int) -> str:
    if seconds <= 0 or seconds >= 8640000:
        return "∞"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _is_bencoded_torrent(payload: bytes) -> bool:
    """Return True when payload begins like a bencoded torrent dictionary."""
    stripped = payload.lstrip()
    return len(stripped) > 2 and stripped[:1] == b"d" and 48 <= stripped[1] <= 57


def _redact_url(url: str) -> str:
    """Strip query/apikey parameters from URLs before writing log lines."""
    raw = (url or "").strip()
    return raw.split("?", 1)[0]


def build_magnet_fallback(
    magnet_url: Optional[str] = None,
    info_hash: Optional[str] = None,
    title: Optional[str] = None,
) -> Optional[str]:
    """Return an explicit magnet URI or synthesize one from an infoHash + title."""
    raw_magnet = (magnet_url or "").strip()
    if is_valid_magnet_uri(raw_magnet):
        return raw_magnet
    ih = (info_hash or "").strip()
    if not ih or has_control_chars(ih):
        return None
    magnet = f"magnet:?xt=urn:btih:{ih}"
    if title and not has_control_chars(title):
        magnet += f"&dn={quote(title)}"
    return magnet


class QBittorrentClient:
    def __init__(self):
        self._cookie: Optional[str] = None

    @property
    def base_url(self) -> str:
        return (settings.QBITTORRENT_URL or "").rstrip("/")

    @base_url.setter
    def base_url(self, value: str) -> None:
        settings.QBITTORRENT_URL = value

    @property
    def username(self) -> str:
        return settings.QBITTORRENT_USERNAME

    @username.setter
    def username(self, value: str) -> None:
        settings.QBITTORRENT_USERNAME = value

    @property
    def password(self) -> str:
        return settings.QBITTORRENT_PASSWORD

    @password.setter
    def password(self, value: str) -> None:
        settings.QBITTORRENT_PASSWORD = value

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers(self) -> Dict[str, str]:
        return {"Cookie": self._cookie} if self._cookie else {}

    async def login(self) -> bool:
        client = get_telemetry_client()
        try:
            res = await client.post(
                self._url("/api/v2/auth/login"),
                data={"username": self.username, "password": self.password},
                timeout=1.5,
            )
            if res.status_code == 200 and "Fails" not in res.text:
                cookies = res.headers.get_list("set-cookie")
                if cookies:
                    self._cookie = "; ".join([c.split(";")[0] for c in cookies])
                return True
            return False
        except Exception:
            return False

    async def check_health(self) -> Tuple[bool, Optional[str], Optional[int]]:
        try:
            client = get_telemetry_client()
            res = await client.get(
                self._url("/api/v2/app/version"),
                headers=self._headers(),
                timeout=1.5,
            )
            if res.status_code == 200:
                return True, res.text.strip(), 200
            if res.status_code == 403:  # Needs login
                logged_in = await self.login()
                return logged_in, "qBittorrent (Auth)", 200 if logged_in else 403
            return False, None, res.status_code
        except Exception:
            return False, None, None

    async def get_transfer_info(self) -> Dict[str, Any]:
        """Returns dl_info_speed, up_info_speed, and free space."""
        client = get_telemetry_client()
        for attempt in range(2):
            try:
                res = await client.get(
                    self._url("/api/v2/transfer/info"),
                    headers=self._headers(),
                    timeout=1.5,
                )
                if res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                if res.status_code == 200:
                    return res.json()
            except Exception:
                break
        return {"dl_info_speed": 0, "up_info_speed": 0}

    async def get_disk_free_space(self) -> int:
        """Returns free space in bytes from sync/maindata."""
        client = get_telemetry_client()
        for attempt in range(2):
            try:
                res = await client.get(
                    self._url("/api/v2/sync/maindata?rid=0"),
                    headers=self._headers(),
                    timeout=1.5,
                )
                if res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                if res.status_code == 200:
                    data = res.json()
                    server_state = data.get("server_state", {})
                    return server_state.get("free_space_on_disk", 0)
            except Exception:
                break
        return 0

    async def _read_bounded_body(self, res: httpx.Response) -> Optional[bytes]:
        """Read streaming HTTP body up to MAX_TORRENT_BYTES, aborting early on oversized payloads."""
        cl_raw = (res.headers.get("content-length") or "").strip()
        if cl_raw:
            try:
                if int(cl_raw) > MAX_TORRENT_BYTES:
                    logger.warning("Torrent payload exceeds size limit (%s bytes)", cl_raw)
                    return None
            except ValueError:
                pass
        buf = bytearray()
        async for chunk in res.aiter_bytes(65536):
            if len(buf) + len(chunk) > MAX_TORRENT_BYTES:
                logger.warning("Torrent stream exceeded %d-byte limit; aborting read", MAX_TORRENT_BYTES)
                return None
            buf.extend(chunk)
        return bytes(buf)

    async def _resolve_torrent_payload(
        self,
        urls: str,
        magnet_url: Optional[str] = None,
        info_hash: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[bytes], Optional[str]]:
        """Resolves a magnet link or downloads a .torrent payload via Muuvies.

        Remote qBittorrent cannot resolve internal Docker tunnel hostnames like
        `seedbox-tunnel`, so HTTP(S) torrent links must be fetched server-side
        (following HTTP redirects or catching `magnet:` redirects) and uploaded
        as multipart `.torrent` files, falling back to `magnet_url` or
        `info_hash` when Prowlarr fails or times out.
        """
        target = (urls or "").strip()
        if not target:
            fallback = build_magnet_fallback(magnet_url=magnet_url, info_hash=info_hash, title=title)
            if fallback:
                return fallback, None, None
            return None, None, "Prowlarr .torrent download failed (and no magnet/infoHash fallback available)."

        if has_control_chars(target):
            return None, None, "Invalid control characters in download URL."

        if target.lower().startswith("magnet:"):
            return target, None, None

        lower_target = target.lower()
        if not (lower_target.startswith("http://") or lower_target.startswith("https://")):
            return None, None, "Only http, https, or magnet download URLs are allowed."

        started_on_prowlarr = is_prowlarr_origin(target)
        valid_init, reason = await validate_download_url(target, allow_prowlarr=started_on_prowlarr)
        if not valid_init:
            logger.warning("Blocked unsafe download_url %s: %s", _redact_url(target), reason)
            return None, None, reason

        client = get_search_client()
        dl_timeout = httpx.Timeout(18.0, connect=4.0)
        for dl_attempt in range(2):
            current_url = target
            stay_on_prowlarr = started_on_prowlarr
            retryable_failure = False
            blocked_redirect_reason: Optional[str] = None
            try:
                # Keep manual redirect handling so `302 -> magnet:` does not trigger httpx TooManyRedirects
                # and every redirect hop is SSRF-validated.
                for _ in range(MAX_REDIRECT_HOPS):
                    if isinstance(client, httpx.AsyncClient):
                        async with client.stream(
                            "GET",
                            current_url,
                            follow_redirects=False,
                            timeout=dl_timeout,
                        ) as res:
                            status_code = res.status_code
                            headers = res.headers
                            body_bytes = (
                                await self._read_bounded_body(res)
                                if status_code == 200
                                else b""
                            )
                    else:
                        res = await client.get(current_url, follow_redirects=False, timeout=dl_timeout)
                        status_code = res.status_code
                        headers = res.headers
                        if status_code == 200:
                            cl_raw = (headers.get("content-length") or "").strip()
                            if cl_raw:
                                try:
                                    if int(cl_raw) > MAX_TORRENT_BYTES:
                                        logger.warning("Torrent payload exceeds size limit (%s bytes)", cl_raw)
                                        break
                                except ValueError:
                                    pass
                            raw_content = res.content or b""
                            if len(raw_content) > MAX_TORRENT_BYTES:
                                logger.warning("Torrent payload exceeded %d-byte limit", MAX_TORRENT_BYTES)
                                break
                            body_bytes = raw_content
                        else:
                            body_bytes = b""

                    if status_code in (301, 302, 303, 307, 308):
                        location = (headers.get("location") or "").strip()
                        if not location:
                            break
                        if has_control_chars(location):
                            blocked_redirect_reason = "Invalid control characters in redirect target."
                            break
                        if location.lower().startswith("magnet:"):
                            return location, None, None
                        next_url = urljoin(current_url, location)
                        stay_on_prowlarr = stay_on_prowlarr and is_prowlarr_origin(next_url)
                        valid_hop, hop_reason = await validate_download_url(
                            next_url,
                            allow_prowlarr=stay_on_prowlarr,
                        )
                        if not valid_hop:
                            logger.warning(
                                "Blocked unsafe redirect from %s to %s: %s",
                                _redact_url(current_url),
                                _redact_url(next_url),
                                hop_reason,
                            )
                            blocked_redirect_reason = hop_reason
                            break
                        current_url = next_url
                        continue

                    if status_code == 200 and body_bytes:
                        if _is_bencoded_torrent(body_bytes):
                            return None, body_bytes, None
                        logger.warning(
                            "Downloaded payload from %s is not a valid bencoded .torrent (first bytes=%r)",
                            _redact_url(current_url),
                            body_bytes[:32],
                        )
                        break

                    if status_code != 200:
                        logger.warning("Failed to fetch .torrent from %s: HTTP %s", _redact_url(current_url), status_code)
                        if status_code >= 500:
                            retryable_failure = True
                    break
            except Exception as exc:
                logger.warning(
                    "Error fetching .torrent from %s (attempt %d/2): %s (%r)",
                    _redact_url(current_url),
                    dl_attempt + 1,
                    type(exc).__name__,
                    exc,
                )
                retryable_failure = True

            if blocked_redirect_reason:
                return None, None, blocked_redirect_reason

            if not retryable_failure:
                break

        fallback = build_magnet_fallback(magnet_url=magnet_url, info_hash=info_hash, title=title)
        if fallback:
            logger.info("Using magnet fallback after Prowlarr .torrent failure for %s", title or _redact_url(current_url))
            return fallback, None, None
        return None, None, "Prowlarr .torrent download failed (and no magnet/infoHash fallback available)."

    async def add_torrent_with_status(
        self,
        urls: str,
        category: str = "Movies",
        paused: bool = False,
        magnet_url: Optional[str] = None,
        info_hash: Optional[str] = None,
        title: Optional[str] = None,
        sequential: bool = False,
    ) -> AddResult:
        """Adds a torrent via .torrent upload or magnet fallback, returning (ok, error_detail, http_status).

        ``sequential`` turns on sequential download and first/last-piece priority
        for this torrent only, so it can be watched while it downloads. The
        result's ``info_hash`` is read from the .torrent or magnet when possible.
        """
        magnet_or_url, torrent_bytes, resolve_err = await self._resolve_torrent_payload(
            urls,
            magnet_url=magnet_url,
            info_hash=info_hash,
            title=title,
        )
        if resolve_err or (not magnet_or_url and not torrent_bytes):
            return AddResult(False, (resolve_err or "Prowlarr .torrent download failed (and no magnet/infoHash fallback available)."), 422)

        added_hash = (
            (torrent_info_hash(torrent_bytes) if torrent_bytes else None)
            or (magnet_info_hash(magnet_or_url) if magnet_or_url else None)
            or normalize_info_hash(info_hash)
        )

        client = get_search_client()
        flag_val = "true" if paused else "false"
        base_data = {
            "category": category,
            "paused": flag_val,
            "stopped": flag_val,
            "autoTMM": "true",
        }
        if sequential:
            base_data["sequentialDownload"] = "true"
            base_data["firstLastPiecePrio"] = "true"
        last_error = "qBittorrent is unreachable."
        for attempt in range(2):
            try:
                if torrent_bytes:
                    res = await client.post(
                        self._url("/api/v2/torrents/add"),
                        headers=self._headers(),
                        data=base_data,
                        files={"torrents": ("release.torrent", torrent_bytes, "application/x-bittorrent")},
                        timeout=6.0,
                    )
                else:
                    res = await client.post(
                        self._url("/api/v2/torrents/add"),
                        headers=self._headers(),
                        data={**base_data, "urls": magnet_or_url},
                        timeout=4.0,
                    )
                if res.status_code == 403 and attempt == 0:
                    logged_in = await self.login()
                    if not logged_in:
                        break
                    continue
                if res.status_code == 200 and "Fails" not in res.text:
                    return AddResult(True, None, 200, added_hash)
                return AddResult(False, f"qBittorrent rejected torrent (HTTP {res.status_code}).", 502)
            except Exception as exc:
                logger.warning(
                    "qBittorrent /torrents/add failed: %s (%r)",
                    type(exc).__name__,
                    exc,
                )
                break
        return AddResult(False, last_error, 502)

    async def add_torrent_with_result(
        self,
        urls: str,
        category: str = "Movies",
        paused: bool = False,
        magnet_url: Optional[str] = None,
        info_hash: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Adds a torrent via .torrent bytes upload or magnet fallback, returning (ok, error_detail)."""
        ok, error_detail, _ = await self.add_torrent_with_status(
            urls=urls,
            category=category,
            paused=paused,
            magnet_url=magnet_url,
            info_hash=info_hash,
            title=title,
        )
        return ok, error_detail

    async def add_torrent(
        self,
        urls: str,
        category: str = "Movies",
        paused: bool = False,
        magnet_url: Optional[str] = None,
        info_hash: Optional[str] = None,
        title: Optional[str] = None,
    ) -> bool:
        """Adds a torrent via .torrent bytes upload (for HTTP URLs) or magnet link."""
        ok, _ = await self.add_torrent_with_result(
            urls=urls,
            category=category,
            paused=paused,
            magnet_url=magnet_url,
            info_hash=info_hash,
            title=title,
        )
        return ok

    async def get_queue(self, filter_status: str = "all", force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Returns active torrent queue items (micro-cached 2s)."""
        from backend.app.services.cache import queue_cache

        cache_key = f"qb_queue:{filter_status}"
        if not force_refresh:
            cached = queue_cache.get(cache_key)
            if cached is not None:
                return cached

        client = get_telemetry_client()
        for attempt in range(2):
            try:
                params = {"filter": filter_status}
                res = await client.get(
                    self._url("/api/v2/torrents/info"),
                    headers=self._headers(),
                    params=params,
                    timeout=1.5,
                )
                if res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                if res.status_code == 200:
                    data = res.json()
                    queue_cache.set(cache_key, data)
                    return data
            except Exception:
                break
        return []

    async def _get_json(self, path: str, params: Dict[str, str], timeout: float = 3.0) -> Any:
        """GET a qBittorrent API path, re-logging in once on 403. Returns parsed JSON or None."""
        client = get_telemetry_client()
        for attempt in range(2):
            try:
                res = await client.get(self._url(path), headers=self._headers(), params=params, timeout=timeout)
                if res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                if res.status_code == 200:
                    return res.json()
                return None
            except Exception as exc:
                logger.warning("qBittorrent %s failed: %s", path, type(exc).__name__)
                return None
        return None

    async def _post(self, path: str, data: Dict[str, str], timeout: float = 4.0) -> bool:
        client = get_search_client()
        for attempt in range(2):
            try:
                res = await client.post(self._url(path), headers=self._headers(), data=data, timeout=timeout)
                if res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                return res.status_code == 200
            except Exception as exc:
                logger.warning("qBittorrent %s failed: %s", path, type(exc).__name__)
                return False
        return False

    async def get_torrent(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """One torrent's /torrents/info entry, or None."""
        rows = await self._get_json("/api/v2/torrents/info", {"hashes": torrent_hash})
        return rows[0] if isinstance(rows, list) and rows else None

    async def get_torrent_files(self, torrent_hash: str) -> List[Dict[str, Any]]:
        rows = await self._get_json("/api/v2/torrents/files", {"hash": torrent_hash})
        return rows if isinstance(rows, list) else []

    async def get_piece_states(self, torrent_hash: str) -> List[int]:
        rows = await self._get_json("/api/v2/torrents/pieceStates", {"hash": torrent_hash}, timeout=4.0)
        return [int(x) for x in rows] if isinstance(rows, list) else []

    async def get_torrent_properties(self, torrent_hash: str) -> Dict[str, Any]:
        props = await self._get_json("/api/v2/torrents/properties", {"hash": torrent_hash})
        return props if isinstance(props, dict) else {}

    async def enable_stream_mode(self, torrent_hash: str) -> Optional[Dict[str, Any]]:
        """Turn on sequential download + first/last-piece priority for one torrent.

        qBittorrent only exposes toggles, so read the current flags first and flip
        only what's off. Also starts the torrent if it's stopped and unfinished.
        Returns the refreshed /torrents/info entry, or None if it doesn't exist.
        """
        info = await self.get_torrent(torrent_hash)
        if info is None:
            return None
        if not info.get("seq_dl"):
            await self._post("/api/v2/torrents/toggleSequentialDownload", {"hashes": torrent_hash})
        if not info.get("f_l_piece_prio"):
            await self._post("/api/v2/torrents/toggleFirstLastPiecePrio", {"hashes": torrent_hash})
        state = str(info.get("state") or "").lower()
        if float(info.get("progress") or 0) < 1 and (state.startswith("paused") or state.startswith("stopped")):
            await self.control_torrents("resume", [torrent_hash])
        return await self.get_torrent(torrent_hash) or info

    async def control_torrents(self, action: str, hashes: List[str], delete_files: bool = False) -> bool:
        """Performs action: pause, resume, or delete (supports qBittorrent v5 stop/start and v4 pause/resume)."""
        hashes_str = "|".join(hashes)
        client = get_search_client()
        action_endpoints = {
            "pause": ["/api/v2/torrents/stop", "/api/v2/torrents/pause"],
            "resume": ["/api/v2/torrents/start", "/api/v2/torrents/resume"],
            "delete": ["/api/v2/torrents/delete"],
        }
        endpoints = action_endpoints.get(action)
        if not endpoints:
            return False

        for attempt in range(2):
            try:
                payload: Dict[str, str] = {"hashes": hashes_str}
                if action == "delete":
                    payload["deleteFiles"] = "true" if delete_files else "false"

                res = None
                for endpoint in endpoints:
                    res = await client.post(
                        self._url(endpoint),
                        headers=self._headers(),
                        data=payload,
                        timeout=4.0,
                    )
                    if res.status_code != 404:
                        break

                if res is not None and res.status_code == 403 and attempt == 0:
                    await self.login()
                    continue
                return res is not None and res.status_code == 200
            except Exception:
                break
        return False


qb_client = QBittorrentClient()

