"""Bounded, cookie-less, SSRF-hardened Adult Vault pre-download preview fetcher."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from collections import OrderedDict, deque
from html.parser import HTMLParser
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from backend.app.config import settings
from backend.app.services.http_clients import get_preview_client
from backend.app.services.url_safety import has_control_chars, validate_download_url

logger = logging.getLogger("muuvies.adult_preview")


def _get_preview_sources() -> Dict[str, Dict[str, Any]]:
    """Parse configured ADULT_PREVIEW_SOURCES_JSON mapping indexer names to host/parser rules."""
    raw = (settings.ADULT_PREVIEW_SOURCES_JSON or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    sources: Dict[str, Dict[str, Any]] = {}
    for name, cfg in parsed.items():
        canon = str(name or "").strip()
        if not canon or not isinstance(cfg, dict):
            continue
        raw_hosts = cfg.get("hosts") or []
        hosts = frozenset(
            str(h).strip().rstrip(".").lower()
            for h in raw_hosts
            if str(h).strip()
        )
        sources[canon] = {
            "hosts": hosts,
            "parser": str(cfg.get("parser") or "gallery").strip().lower(),
            "cover_hint": str(cfg.get("cover_hint") or "").strip().lower(),
            "shot_hint": str(cfg.get("shot_hint") or "").strip().lower(),
            "poster_fallback": bool(cfg.get("poster_fallback", False)),
        }
    return sources


def _get_image_exact_hosts() -> frozenset[str]:
    return frozenset(
        h.strip().rstrip(".").lower()
        for h in (settings.ADULT_PREVIEW_IMAGE_HOSTS or "").split(",")
        if h.strip()
    )


def _get_image_subdomain_hosts() -> Tuple[str, ...]:
    return tuple(
        h.strip().rstrip(".").lower()
        for h in (settings.ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS or "").split(",")
        if h.strip()
    )


def get_preview_privacy_hosts() -> Tuple[str, ...]:
    """Return all configured page and image hosts that must be scrubbed from telemetry."""
    hosts: list[str] = []
    for cfg in _get_preview_sources().values():
        hosts.extend(sorted(cfg["hosts"]))
    hosts.extend(sorted(_get_image_exact_hosts()))
    hosts.extend(_get_image_subdomain_hosts())
    return tuple(dict.fromkeys(h for h in hosts if h))


def supports_poster_fallback(indexer: Optional[str]) -> bool:
    """Return True if the canonical preview indexer allows falling back to poster_url."""
    canon = canonical_preview_indexer(indexer)
    if canon is None:
        return False
    return bool(_get_preview_sources().get(canon, {}).get("poster_fallback", False))

MAX_PREVIEW_PAGE_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_IMAGE_BYTES = 2 * 1024 * 1024
MIN_PREVIEW_IMAGE_BYTES = 4 * 1024
PAGE_TIMEOUT_SECONDS = 6.0
IMAGE_TIMEOUT_SECONDS = 5.0
MAX_REDIRECT_HOPS = 5
MAX_PREVIEW_IMAGES = 5

PREVIEW_CACHE_TTL_SECONDS = 900
MAX_LIST_CACHE_ENTRIES = 200
MAX_IMAGE_CACHE_BYTES = 32 * 1024 * 1024

RATE_LIMIT_MAX_PREVIEWS = 30
RATE_LIMIT_WINDOW_SECONDS = 600
MAX_RATE_LIMIT_SESSIONS = 512
MAX_CONCURRENT_FETCHES_PER_SESSION = 3

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PreviewError(Exception):
    """Base error for adult preview failures."""


class PreviewNotFoundError(PreviewError):
    """Requested preview payload or host is unavailable/disallowed (maps to 404)."""


class PreviewBlockedError(PreviewError):
    """Outbound URL/redirect failed host or SSRF validation."""


class PreviewTooLargeError(PreviewError):
    """Upstream page or image exceeded the 2 MB cap."""


class PreviewInvalidImageError(PreviewError):
    """Image was smaller than 4 KB (icon/smiley/tracker spacer)."""


class PreviewUnsupportedMediaError(PreviewError):
    """Bytes did not match JPEG, PNG, or WebP magic headers."""


class PreviewRateLimitError(PreviewError):
    """Per-session preview budget exceeded."""


class PreviewUpstreamError(PreviewError):
    """Transient network error or non-200 response from upstream host."""


# In-memory caches & limits (never persisted to data/ or SQLite).
_list_cache: "OrderedDict[Tuple[str, str], Tuple[float, List[Dict[str, str]]]]" = OrderedDict()
_image_cache: "OrderedDict[str, Tuple[float, str, bytes]]" = OrderedDict()
_image_cache_bytes = 0
_rate_limit_windows: "OrderedDict[str, Deque[float]]" = OrderedDict()
_session_semaphores: Dict[str, asyncio.Semaphore] = {}
_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None


def clear_preview_caches() -> None:
    """Clear all in-memory preview caches, rate limits, and semaphores (primarily for tests)."""
    global _image_cache_bytes, _semaphore_loop
    _list_cache.clear()
    _image_cache.clear()
    _image_cache_bytes = 0
    _rate_limit_windows.clear()
    _session_semaphores.clear()
    _semaphore_loop = None


def _get_session_semaphore(session_key: str) -> asyncio.Semaphore:
    global _semaphore_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _semaphore_loop is not loop or (loop is not None and loop.is_closed()):
        _session_semaphores.clear()
        _semaphore_loop = loop

    key = session_key or "_anon"
    sem = _session_semaphores.get(key)
    if sem is None:
        if len(_session_semaphores) >= MAX_RATE_LIMIT_SESSIONS:
            oldest = next(iter(_session_semaphores))
            _session_semaphores.pop(oldest, None)
        sem = asyncio.Semaphore(MAX_CONCURRENT_FETCHES_PER_SESSION)
        _session_semaphores[key] = sem
    return sem


def _enforce_preview_rate_limit(session_key: str) -> None:
    now = time.monotonic()
    key = session_key or "_anon"
    window = _rate_limit_windows.get(key)
    if window is None:
        if len(_rate_limit_windows) >= MAX_RATE_LIMIT_SESSIONS:
            _rate_limit_windows.popitem(last=False)
        window = deque()
        _rate_limit_windows[key] = window
    else:
        _rate_limit_windows.move_to_end(key)

    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    while window and window[0] <= cutoff:
        window.popleft()

    if len(window) >= RATE_LIMIT_MAX_PREVIEWS:
        raise PreviewRateLimitError("Too many preview requests. Try again shortly.")
    window.append(now)


def canonical_preview_indexer(indexer: Optional[str]) -> Optional[str]:
    """Normalize an indexer name against configured ADULT_PREVIEW_SOURCES_JSON entries."""
    low = (indexer or "").strip().lower()
    if not low:
        return None
    for canon in _get_preview_sources():
        if canon.lower() in low:
            return canon
    return None


def _extract_safe_http_host(url: str) -> Optional[str]:
    raw = (url or "").strip()
    if not raw or has_control_chars(raw):
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if (parsed.scheme or "").lower() not in ("http", "https"):
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    try:
        if parsed.port is not None and parsed.port not in (80, 443):
            return None
    except ValueError:
        return None
    return host


def is_allowed_page_url(url: str, indexer: str) -> bool:
    """Return True if url is HTTP(S) on the strict page host allowlist for indexer."""
    canon = canonical_preview_indexer(indexer)
    if canon is None:
        return False
    host = _extract_safe_http_host(url)
    if not host:
        return False
    allowed_hosts = _get_preview_sources().get(canon, {}).get("hosts") or frozenset()
    return host in allowed_hosts


def is_allowed_image_url(url: str) -> bool:
    """Return True if url is HTTP(S) on an allowlisted image CDN host."""
    host = _extract_safe_http_host(url)
    if not host:
        return False
    if host in _get_image_exact_hosts():
        return True
    return any(host == base or host.endswith(f".{base}") for base in _get_image_subdomain_hosts())


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Sniff JPEG, PNG, or WebP from magic bytes (ignores response Content-Type)."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _BBCodePostImgParser(HTMLParser):
    """Extract allowlisted postImg URLs from the first post_body (or document fallback)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._post_state = 0  # 0=before first post, 1=inside first post, 2=after first post
        self._post_depth = 0
        self.first_post_urls: List[str] = []
        self.fallback_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = {c.lower() for c in attr_map.get("class", "").split()}

        if self._post_state == 0 and (classes & {"post_body", "post-body", "postbody"}):
            self._post_state = 1
            self._post_depth = 1
        elif self._post_state == 1 and tag_l not in _VOID_HTML_TAGS:
            self._post_depth += 1

        if tag_l == "var" and "postimg" in classes:
            title_url = attr_map.get("title", "").strip()
            if title_url:
                if self._post_state == 1:
                    self.first_post_urls.append(title_url)
                elif self._post_state == 0:
                    self.fallback_urls.append(title_url)

    def handle_endtag(self, tag: str) -> None:
        if self._post_state == 1 and tag.lower() not in _VOID_HTML_TAGS:
            self._post_depth -= 1
            if self._post_depth <= 0:
                self._post_state = 2


def parse_bbcode_preview_images(html: str, page_url: str) -> List[Dict[str, str]]:
    """Parse up to 5 BBCode postImg images (cover first), keeping only allowlisted hosts."""
    parser = _BBCodePostImgParser()
    parser.feed(html or "")
    parser.close()

    raw_urls = parser.first_post_urls if parser._post_state > 0 else parser.fallback_urls
    big_urls: List[str] = []
    other_urls: List[str] = []
    seen: set[str] = set()

    for raw in raw_urls:
        resolved = urljoin(page_url, raw.strip())
        if not is_allowed_image_url(resolved) or resolved in seen:
            continue
        seen.add(resolved)
        lowered = resolved.lower()
        if "/big/" in lowered:
            big_urls.append(resolved)
        else:
            other_urls.append(resolved)

    ordered: List[Dict[str, str]] = []
    if big_urls:
        ordered.append({"url": big_urls[0], "role": "cover"})
        for u in big_urls[1:] + other_urls:
            ordered.append({"url": u, "role": "shot"})
    elif other_urls:
        ordered.append({"url": other_urls[0], "role": "cover"})
        for u in other_urls[1:]:
            ordered.append({"url": u, "role": "shot"})

    return ordered[:MAX_PREVIEW_IMAGES]


class _GalleryHTMLParser(HTMLParser):
    """Extract candidate <img> URLs from release gallery HTML pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.img_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "img":
            return
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs}
        candidate = attr_map.get("data-src") or attr_map.get("src") or ""
        if candidate and not candidate.lower().startswith("data:"):
            self.img_urls.append(candidate)


def parse_gallery_preview_images(
    html: str,
    page_url: str,
    poster_url: Optional[str] = None,
    *,
    cover_hint: str = "",
    shot_hint: str = "",
) -> List[Dict[str, str]]:
    """Parse up to 5 gallery images (cover first), falling back to poster_url."""
    parser = _GalleryHTMLParser()
    parser.feed(html or "")
    parser.close()

    covers: List[str] = []
    shots_primary: List[str] = []
    shots_other: List[str] = []
    seen: set[str] = set()
    cover_h = cover_hint.strip().lower()
    shot_h = shot_hint.strip().lower()

    for raw in parser.img_urls:
        resolved = urljoin(page_url, raw)
        if not is_allowed_image_url(resolved) or resolved in seen:
            continue
        seen.add(resolved)
        lowered = resolved.lower()
        if cover_h and cover_h in lowered:
            covers.append(resolved)
        elif shot_h and shot_h in lowered:
            shots_primary.append(resolved)
        else:
            shots_other.append(resolved)

    clean_poster: Optional[str] = None
    if poster_url:
        candidate = poster_url.strip()
        if is_allowed_image_url(candidate):
            clean_poster = candidate

    ordered: List[Dict[str, str]] = []
    used: set[str] = set()
    cover_url = covers[0] if covers else clean_poster
    if cover_url:
        ordered.append({"url": cover_url, "role": "cover"})
        used.add(cover_url)

    for shot in shots_primary + covers[1:] + shots_other:
        if len(ordered) >= MAX_PREVIEW_IMAGES:
            break
        if shot not in used:
            ordered.append({"url": shot, "role": "shot"})
            used.add(shot)

    return ordered[:MAX_PREVIEW_IMAGES]


async def _read_bounded_stream(res: httpx.Response, max_bytes: int) -> bytes:
    cl_raw = (res.headers.get("content-length") or "").strip()
    if cl_raw:
        try:
            if int(cl_raw) > max_bytes:
                raise PreviewTooLargeError("Preview response exceeds size limit.")
        except ValueError:
            pass
    buf = bytearray()
    async for chunk in res.aiter_bytes(65536):
        if len(buf) + len(chunk) > max_bytes:
            raise PreviewTooLargeError("Preview stream exceeded size limit.")
        buf.extend(chunk)
    return bytes(buf)


async def _fetch_bounded(
    url: str,
    *,
    host_validator: Callable[[str], bool],
    max_bytes: int,
    timeout_seconds: float,
    accept_header: str,
    session_key: str,
) -> bytes:
    """Fetch HTTP(S) payload with strict host allowlist, SSRF re-check per hop, no cookies, no Referer."""
    client = get_preview_client()
    timeout = httpx.Timeout(timeout_seconds, connect=min(4.0, timeout_seconds))
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": accept_header,
        "Accept-Language": "en-US,en;q=0.9",
    }
    sem = _get_session_semaphore(session_key)
    current_url = url

    for _ in range(MAX_REDIRECT_HOPS + 1):
        if not host_validator(current_url):
            raise PreviewBlockedError("Preview URL host is not allowlisted.")

        ok_ssrf, reason = await validate_download_url(current_url, allow_prowlarr=False)
        if not ok_ssrf:
            raise PreviewBlockedError(reason or "Preview URL failed SSRF validation.")

        try:
            async with sem:
                async with client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                    timeout=timeout,
                ) as res:
                    status_code = res.status_code
                    res_headers = res.headers
                    body = await _read_bounded_stream(res, max_bytes) if status_code == 200 else b""
        except PreviewError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise PreviewUpstreamError(f"Preview upstream failed ({type(exc).__name__}).") from exc

        if status_code in (301, 302, 303, 307, 308):
            location = (res_headers.get("location") or "").strip()
            if not location or has_control_chars(location):
                raise PreviewBlockedError("Invalid redirect target.")
            current_url = urljoin(current_url, location)
            continue

        if status_code != 200:
            raise PreviewUpstreamError(f"Preview upstream returned HTTP {status_code}.")
        return body

    raise PreviewUpstreamError("Too many preview redirects.")


def _get_cached_list(cache_key: Tuple[str, str]) -> Optional[List[Dict[str, str]]]:
    entry = _list_cache.get(cache_key)
    if entry is None:
        return None
    expires_at, images = entry
    if expires_at <= time.monotonic():
        _list_cache.pop(cache_key, None)
        return None
    _list_cache.move_to_end(cache_key)
    return [dict(img) for img in images]


def _set_cached_list(cache_key: Tuple[str, str], images: List[Dict[str, str]]) -> None:
    _list_cache[cache_key] = (
        time.monotonic() + PREVIEW_CACHE_TTL_SECONDS,
        [dict(img) for img in images],
    )
    _list_cache.move_to_end(cache_key)
    while len(_list_cache) > MAX_LIST_CACHE_ENTRIES:
        _list_cache.popitem(last=False)


def _get_cached_image(img_url: str) -> Optional[Tuple[bytes, str]]:
    global _image_cache_bytes
    entry = _image_cache.get(img_url)
    if entry is None:
        return None
    expires_at, mime, data = entry
    if expires_at <= time.monotonic():
        _image_cache.pop(img_url, None)
        _image_cache_bytes = max(0, _image_cache_bytes - len(data))
        return None
    _image_cache.move_to_end(img_url)
    return data, mime


def _set_cached_image(img_url: str, data: bytes, mime: str) -> None:
    global _image_cache_bytes
    size = len(data)
    if size > MAX_IMAGE_CACHE_BYTES:
        return
    old = _image_cache.pop(img_url, None)
    if old is not None:
        _image_cache_bytes = max(0, _image_cache_bytes - len(old[2]))
    while _image_cache and (_image_cache_bytes + size > MAX_IMAGE_CACHE_BYTES):
        _, (_, _, evicted) = _image_cache.popitem(last=False)
        _image_cache_bytes = max(0, _image_cache_bytes - len(evicted))
    _image_cache[img_url] = (time.monotonic() + PREVIEW_CACHE_TTL_SECONDS, mime, data)
    _image_cache_bytes += size


async def resolve_preview_images(
    *,
    indexer: str,
    info_url: Optional[str],
    poster_url: Optional[str] = None,
    session_key: str = "",
) -> List[Dict[str, str]]:
    """Resolve up to 5 cover/screenshot URLs for an adult tracker release."""
    t0 = time.perf_counter()

    canon_indexer = canonical_preview_indexer(indexer)
    if canon_indexer is None:
        raise PreviewNotFoundError("Preview indexer not supported.")

    src_cfg = _get_preview_sources().get(canon_indexer, {})
    allow_poster_fallback = bool(src_cfg.get("poster_fallback", False))

    clean_info = (info_url or "").strip()
    clean_poster = (poster_url or "").strip() or None
    if clean_poster and not is_allowed_image_url(clean_poster):
        clean_poster = None

    if not clean_info:
        if allow_poster_fallback and clean_poster:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "Adult preview resolved indexer=%s images=1 cached=false elapsed_ms=%.1f",
                canon_indexer,
                elapsed_ms,
            )
            return [{"url": clean_poster, "role": "cover"}]
        raise PreviewNotFoundError("Preview target unavailable.")

    if not is_allowed_page_url(clean_info, canon_indexer):
        raise PreviewNotFoundError("Preview host is not allowlisted.")

    cache_key = (canon_indexer, clean_info)
    cached_list = _get_cached_list(cache_key)
    if cached_list is not None:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "Adult preview resolved indexer=%s images=%d cached=true elapsed_ms=%.1f",
            canon_indexer,
            len(cached_list),
            elapsed_ms,
        )
        return cached_list

    _enforce_preview_rate_limit(session_key)

    try:
        page_bytes = await _fetch_bounded(
            clean_info,
            host_validator=lambda u: is_allowed_page_url(u, canon_indexer),
            max_bytes=MAX_PREVIEW_PAGE_BYTES,
            timeout_seconds=PAGE_TIMEOUT_SECONDS,
            accept_header="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            session_key=session_key,
        )
    except PreviewUpstreamError:
        if allow_poster_fallback and clean_poster:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "Adult preview fallback indexer=%s images=1 cached=false elapsed_ms=%.1f",
                canon_indexer,
                elapsed_ms,
            )
            return [{"url": clean_poster, "role": "cover"}]
        raise

    html_text = page_bytes.decode("utf-8", errors="replace")
    if src_cfg.get("parser") == "bbcode":
        images = parse_bbcode_preview_images(html_text, clean_info)
    else:
        images = parse_gallery_preview_images(
            html_text,
            clean_info,
            poster_url=clean_poster,
            cover_hint=str(src_cfg.get("cover_hint") or ""),
            shot_hint=str(src_cfg.get("shot_hint") or ""),
        )

    _set_cached_list(cache_key, images)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "Adult preview resolved indexer=%s images=%d cached=false elapsed_ms=%.1f",
        canon_indexer,
        len(images),
        elapsed_ms,
    )
    return images


async def fetch_preview_image(
    *,
    indexer: str,
    img_url: str,
    session_key: str = "",
) -> Tuple[bytes, str]:
    """Fetch, size-check, and magic-sniff an allowlisted preview image."""
    t0 = time.perf_counter()
    clean_url = (img_url or "").strip()
    if not clean_url or not is_allowed_image_url(clean_url):
        raise PreviewNotFoundError("Preview image host is not allowlisted.")

    cached = _get_cached_image(clean_url)
    if cached is not None:
        data, mime = cached
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "Adult preview image served indexer=%s mime=%s bytes=%d cached=true elapsed_ms=%.1f",
            indexer or "Unknown",
            mime,
            len(data),
            elapsed_ms,
        )
        return data, mime

    data = await _fetch_bounded(
        clean_url,
        host_validator=is_allowed_image_url,
        max_bytes=MAX_PREVIEW_IMAGE_BYTES,
        timeout_seconds=IMAGE_TIMEOUT_SECONDS,
        accept_header="image/webp,image/png,image/jpeg,*/*;q=0.8",
        session_key=session_key,
    )

    if len(data) < MIN_PREVIEW_IMAGE_BYTES:
        raise PreviewInvalidImageError("Preview image too small.")

    mime = sniff_image_mime(data)
    if mime is None:
        raise PreviewUnsupportedMediaError("Unsupported preview image format.")

    _set_cached_image(clean_url, data, mime)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "Adult preview image served indexer=%s mime=%s bytes=%d cached=false elapsed_ms=%.1f",
        indexer or "Unknown",
        mime,
        len(data),
        elapsed_ms,
    )
    return data, mime

