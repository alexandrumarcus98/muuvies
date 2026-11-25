"""Pure-ASGI default-deny API guard and Adult Vault /api/vault/x/* stealth layer."""

from __future__ import annotations

import re
from http.cookies import SimpleCookie
from typing import Optional

from fastapi.responses import ORJSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.app.config import settings
from backend.app.security import check_vault_unlocked, decode_session_token

# Explicit allowlist of /api/* routes that do not require a standard cookie/bearer session
# at the middleware boundary (public auth/health and Fernet-token stream endpoints).
PUBLIC_API_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/health$"),
    re.compile(r"^/api/auth/config$"),
    re.compile(r"^/api/auth/register$"),
    re.compile(r"^/api/auth/verify-email$"),
    re.compile(r"^/api/auth/resend-verification$"),
    re.compile(r"^/api/auth/forgot-password$"),
    re.compile(r"^/api/auth/reset-password$"),
    re.compile(r"^/api/auth/login$"),
    re.compile(r"^/api/auth/logout$"),
    re.compile(r"^/api/auth/passkeys/login/options$"),
    re.compile(r"^/api/auth/passkeys/login/verify$"),
    re.compile(r"^/api/stream/hls/[^/]+/.+$"),
    re.compile(r"^/api/stream/file/[^/]+$"),
    re.compile(r"^/api/public/trending$"),
    re.compile(r"^/api/public/backdrop/[^/]+$"),
)


def is_public_api_path(path: str) -> bool:
    """Return True when path matches the explicit public / self-gated /api/* allowlist."""
    return any(pattern.fullmatch(path) for pattern in PUBLIC_API_PATTERNS)


def is_vault_only_api_path(path: str) -> bool:
    """Return True for vault-exclusive /api/vault/x/* routes that must return 404 unless unlocked."""
    return path == "/api/vault/x" or path.startswith("/api/vault/x/")


def is_vault_api_path(path: str) -> bool:
    """Return True for any /api/vault or /api/vault/* route (stealth 404 when unauthenticated)."""
    return path == "/api/vault" or path.startswith("/api/vault/")


def _extract_token_from_scope(scope: Scope) -> Optional[str]:
    headers = Headers(scope=scope)
    cookie_header = headers.get("cookie")
    if cookie_header:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(settings.COOKIE_NAME)
        if morsel and morsel.value:
            return morsel.value

    auth_header = headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token
    return None


class APIGuardMiddleware:
    """Fail-closed ASGI middleware enforcing session/vault checks on every /api/* path."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "").upper()
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if not (path == "/api" or path.startswith("/api/")):
            await self.app(scope, receive, send)
            return

        if is_vault_only_api_path(path):
            token = _extract_token_from_scope(scope)
            payload = decode_session_token(token) if token else None
            if not (payload and check_vault_unlocked(payload)):
                resp = ORJSONResponse({"detail": "Not Found"}, status_code=404)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if is_vault_api_path(path):
            token = _extract_token_from_scope(scope)
            payload = decode_session_token(token) if token else None
            if payload is None:
                resp = ORJSONResponse({"detail": "Not Found"}, status_code=404)
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if is_public_api_path(path):
            await self.app(scope, receive, send)
            return

        token = _extract_token_from_scope(scope)
        if not token:
            resp = ORJSONResponse({"detail": "Authentication required"}, status_code=401)
            await resp(scope, receive, send)
            return

        if decode_session_token(token) is None:
            resp = ORJSONResponse({"detail": "Session expired or invalid"}, status_code=401)
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)

