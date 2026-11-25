"""SSRF protection and download URL validation helpers for torrent and *arr pushing."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Tuple, Union
from urllib.parse import urlparse

from backend.app.config import settings

MAX_TORRENT_BYTES = 10 * 1024 * 1024  # 10 MiB max .torrent payload
MAX_REDIRECT_HOPS = 5

_DISALLOWED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "seedbox-tunnel",
    "socks-proxy",
    "challenge-solver",
    "media-search",
    "caddy-proxy",
    "host.docker.internal",
    "gateway.docker.internal",
})

_DISALLOWED_SUFFIXES = (".local", ".internal", ".localhost", ".arpa", ".lan", ".home")


def has_control_chars(value: str) -> bool:
    """Return True if value contains CR, LF, NUL, or other ASCII control characters."""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def is_valid_magnet_uri(value: str) -> bool:
    """Return True for clean magnet: URIs free of control characters."""
    raw = (value or "").strip()
    return raw.lower().startswith("magnet:") and not has_control_chars(raw)


def _is_disallowed_ip(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        if _is_disallowed_ip(ip.ipv4_mapped):
            return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def _effective_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if (parsed.scheme or "").lower() == "https" else 80


def is_prowlarr_origin(url: str) -> bool:
    """Return True when url points to the configured Prowlarr endpoint (or Prowlarr tunnel port)."""
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return False
        port = _effective_port(parsed)

        p_raw = (settings.PROWLARR_URL or "").strip()
        if not p_raw:
            return False
        p_parsed = urlparse(p_raw)
        p_scheme = (p_parsed.scheme or "").lower()
        p_host = (p_parsed.hostname or "").rstrip(".").lower()
        p_port = _effective_port(p_parsed) if p_parsed.hostname else 9696

        if scheme == p_scheme and host == p_host and port == p_port:
            return True
        if host == "seedbox-tunnel" and port == p_port:
            return True
        return False
    except ValueError:
        return False


async def validate_download_url(
    url: str,
    *,
    allow_prowlarr: bool = True,
) -> Tuple[bool, str]:
    """Validate HTTP(S) torrent download URLs against SSRF and internal network targets."""
    raw = (url or "").strip()
    if not raw or has_control_chars(raw):
        return False, "Invalid or malformed download URL."

    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, "Only http and https download URLs are permitted."
        if parsed.username is not None or parsed.password is not None:
            return False, "Credentials in download URLs are not allowed."
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return False, "Download URL is missing a hostname."
        port = _effective_port(parsed)
    except ValueError:
        return False, "Invalid port or syntax in download URL."

    if allow_prowlarr and is_prowlarr_origin(raw):
        return True, ""

    if host in _DISALLOWED_HOSTNAMES or host.endswith(_DISALLOWED_SUFFIXES):
        return False, "Download URL points to a disallowed internal host."

    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if _is_disallowed_ip(ip_literal):
            return False, "Download URL points to a disallowed IP address."
        return True, ""

    if "." not in host:
        return False, "Download URL points to a disallowed single-label host."

    try:
        addr_info = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False, "Could not resolve download URL hostname."

    if not addr_info:
        return False, "Could not resolve download URL hostname."

    for entry in addr_info:
        sockaddr = entry[4]
        if not sockaddr:
            continue
        addr_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved_ip = ipaddress.ip_address(addr_str)
        except ValueError:
            return False, "Resolved address is invalid."
        if _is_disallowed_ip(resolved_ip):
            return False, "Download URL resolves to a disallowed internal IP address."

    return True, ""

