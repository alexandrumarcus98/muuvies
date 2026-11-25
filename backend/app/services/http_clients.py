"""Isolated persistent HTTP/2 connection pools for search/AI vs. seedbox telemetry."""

from __future__ import annotations

import asyncio
import http.cookiejar
from typing import Optional, Tuple
import httpx

_search_client: Optional[httpx.AsyncClient] = None
_telemetry_client: Optional[httpx.AsyncClient] = None
_preview_client: Optional[httpx.AsyncClient] = None
_bound_loop: Optional[asyncio.AbstractEventLoop] = None


def _build_search_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
        follow_redirects=True,
    )


def _build_telemetry_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=15, keepalive_expiry=30.0),
        timeout=httpx.Timeout(1.5),
        follow_redirects=True,
    )


def _build_preview_client(
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> httpx.AsyncClient:
    """Cookie-refusing, non-redirecting pool for Adult Vault preview fetches."""
    client = httpx.AsyncClient(
        http2=False,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0),
        timeout=httpx.Timeout(connect=4.0, read=6.0, write=5.0, pool=5.0),
        follow_redirects=False,
        **({"transport": transport} if transport is not None else {}),
    )
    # Attach policy after AsyncClient init (AsyncClient copies a passed CookieJar and resets policy).
    client.cookies.jar.set_policy(
        http.cookiejar.DefaultCookiePolicy(allowed_domains=[])
    )
    return client


def _current_running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _ensure_loop_match() -> None:
    """Discard stale clients bound to a closed or different event loop (e.g. per-test loops)."""
    global _search_client, _telemetry_client, _preview_client, _bound_loop
    loop = _current_running_loop()
    if _bound_loop is not None and (loop is not _bound_loop or _bound_loop.is_closed()):
        _search_client = None
        _telemetry_client = None
        _preview_client = None
    _bound_loop = loop


async def init_http_clients() -> Tuple[httpx.AsyncClient, httpx.AsyncClient]:
    """Initialize global pooled HTTP clients during FastAPI startup."""
    global _search_client, _telemetry_client, _preview_client, _bound_loop
    _ensure_loop_match()
    if _search_client is None or _search_client.is_closed:
        _search_client = _build_search_client()
    if _telemetry_client is None or _telemetry_client.is_closed:
        _telemetry_client = _build_telemetry_client()
    if _preview_client is None or _preview_client.is_closed:
        _preview_client = _build_preview_client()
    _bound_loop = _current_running_loop()
    return _search_client, _telemetry_client


async def close_http_clients() -> None:
    """Gracefully close pooled HTTP clients on FastAPI shutdown."""
    global _search_client, _telemetry_client, _preview_client, _bound_loop
    loop = _current_running_loop()
    can_aclose = _bound_loop is None or (_bound_loop is loop and not _bound_loop.is_closed())

    if _search_client is not None and not _search_client.is_closed and can_aclose:
        try:
            await _search_client.aclose()
        except RuntimeError:
            pass
    _search_client = None

    if _telemetry_client is not None and not _telemetry_client.is_closed and can_aclose:
        try:
            await _telemetry_client.aclose()
        except RuntimeError:
            pass
    _telemetry_client = None

    if _preview_client is not None and not _preview_client.is_closed and can_aclose:
        try:
            await _preview_client.aclose()
        except RuntimeError:
            pass
    _preview_client = None
    _bound_loop = None


def get_search_client() -> httpx.AsyncClient:
    """Return the shared HTTP/2 search/AI pool, initializing lazily in tests if needed."""
    global _search_client
    _ensure_loop_match()
    if _search_client is None or _search_client.is_closed:
        _search_client = _build_search_client()
    return _search_client


def get_telemetry_client() -> httpx.AsyncClient:
    """Return the shared short-timeout HTTP/2 telemetry/queue pool."""
    global _telemetry_client
    _ensure_loop_match()
    if _telemetry_client is None or _telemetry_client.is_closed:
        _telemetry_client = _build_telemetry_client()
    return _telemetry_client


def get_preview_client() -> httpx.AsyncClient:
    """Return the shared cookie-refusing, non-redirecting Adult Vault preview pool."""
    global _preview_client
    _ensure_loop_match()
    if _preview_client is None or _preview_client.is_closed:
        _preview_client = _build_preview_client()
    return _preview_client
