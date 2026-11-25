"""Route-inventory and default-deny API guard verification tests."""

from __future__ import annotations

import re
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from backend.app.config import settings
from backend.app.main import app
from backend.app.middleware.api_guard import (
    PUBLIC_API_PATTERNS,
    APIGuardMiddleware,
    is_public_api_path,
    is_vault_api_path,
    is_vault_only_api_path,
)
from backend.app.routers._access import vault_router
from backend.app.routers.vault import _require_stealth_vault_user
from backend.app.security import (
    create_session_token,
    get_current_session,
    require_admin,
    require_vault_unlocked,
)

_AUTH_DEPS = frozenset(
    {
        get_current_session,
        require_admin,
        require_vault_unlocked,
        _require_stealth_vault_user,
    }
)


def _collect_dep_calls(dependant) -> set:
    calls = set()
    if getattr(dependant, "call", None) is not None:
        calls.add(dependant.call)
    for sub in getattr(dependant, "dependencies", ()) or ():
        calls.update(_collect_dep_calls(sub))
    return calls


def _sample_path(path_template: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name.endswith(":path") or "subpath" in name:
            return "master.m3u8"
        if name.endswith("_id"):
            return "1"
        return "sample"

    return re.sub(r"\{([^}]+)\}", _replace, path_template)


def _api_routes():
    routes = []
    for route in app.routes:
        effective = getattr(route, "effective_route_contexts", None)
        if callable(effective):
            candidates = list(effective())
        else:
            candidates = [route]
        for candidate in candidates:
            path = getattr(candidate, "path", "")
            if hasattr(candidate, "dependant") and (path == "/api" or path.startswith("/api/")):
                routes.append(candidate)
    return routes


def test_every_api_route_is_allowlisted_or_has_auth_dependency():
    routes = _api_routes()
    assert routes

    matched_patterns = set()
    for route in routes:
        sample = _sample_path(route.path)
        matching = [p for p in PUBLIC_API_PATTERNS if p.fullmatch(sample)]
        for p in matching:
            matched_patterns.add(p)

        dep_calls = _collect_dep_calls(route.dependant)
        has_auth = bool(dep_calls & _AUTH_DEPS)
        assert matching or has_auth, (
            f"API route {route.methods} {route.path} is neither on PUBLIC_API_PATTERNS "
            "nor protected by an explicit auth dependency"
        )

    stale = set(PUBLIC_API_PATTERNS) - matched_patterns
    assert not stale, f"Stale PUBLIC_API_PATTERNS entries with no matching route: {[p.pattern for p in stale]}"


def test_vault_x_routes_require_vault_unlocked():
    for route in _api_routes():
        if is_vault_only_api_path(route.path):
            dep_calls = _collect_dep_calls(route.dependant)
            assert require_vault_unlocked in dep_calls, (
                f"Vault-only route {route.path} is missing require_vault_unlocked"
            )


@pytest.mark.asyncio
async def test_all_private_api_routes_reject_unauthenticated_requests():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for route in _api_routes():
            sample = _sample_path(route.path)
            if is_public_api_path(sample):
                continue
            expected_status = 404 if is_vault_api_path(sample) else 401
            for method in sorted(m for m in (route.methods or set()) if m not in ("HEAD", "OPTIONS")):
                res = await client.request(method, sample)
                assert res.status_code == expected_status, (
                    f"{method} {sample} returned {res.status_code}, expected {expected_status}"
                )


@pytest.mark.asyncio
async def test_vault_routes_preserve_stealth_404_when_unauthenticated():
    origin = f"https://media.{settings.DOMAIN}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        probes = [
            ("POST", "/api/vault/unlock/passkey/options", {}),
            ("GET", "/api/vault/unlock/passkey/options", {}),
            ("POST", "/api/vault/unlock/passkey/verify", {}),
            (
                "POST",
                "/api/vault/unlock/passkey/verify",
                {"json": {"challenge_id": "x", "credential": {}}},
            ),
            ("GET", "/api/vault/unlock/passkey/verify", {}),
            ("GET", "/api/vault/status", {}),
            ("POST", "/api/vault/unlock", {}),
            ("POST", "/api/vault/unlock", {"json": {"pin": "0000"}}),
            ("POST", "/api/vault/lock", {}),
            ("GET", "/api/vault", {}),
            ("GET", "/api/vault/nonexistent", {}),
            (
                "GET",
                "/api/vault/status",
                {"cookies": {settings.COOKIE_NAME: "tampered"}},
            ),
            (
                "POST",
                "/api/vault/unlock/passkey/options",
                {"cookies": {settings.COOKIE_NAME: "tampered"}},
            ),
        ]
        for method, path, kwargs in probes:
            res = await client.request(method, path, **kwargs)
            assert res.status_code == 404, f"{method} {path} returned {res.status_code}, expected 404"

        cors_404 = await client.get("/api/vault/status", headers={"Origin": origin})
        assert cors_404.status_code == 404
        assert cors_404.headers.get("access-control-allow-origin") == origin

        preflight = await client.options(
            "/api/vault/unlock",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers.get("access-control-allow-origin") == origin


def _build_guard_probe_app() -> FastAPI:
    probe_app = FastAPI()
    probe_app.add_middleware(APIGuardMiddleware)
    probe_app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"https://media.{settings.DOMAIN}"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @probe_app.get("/api/forgotten")
    async def forgotten_endpoint():
        return {"ok": True}

    @probe_app.get("/api/stream-chunk")
    async def stream_chunk_endpoint():
        async def _gen():
            yield b"chunk-1:"
            yield b"chunk-2"

        return StreamingResponse(_gen(), media_type="application/octet-stream")

    @probe_app.get("/public-page")
    async def public_page():
        return {"page": True}

    v_router = vault_router(prefix="/api/vault/x")

    @v_router.get("/probe")
    async def vault_probe():
        return {"vault": "secret"}

    probe_app.dependency_overrides[get_current_session] = lambda: {
        "sub": "alice",
        "role": "user",
        "vault_unlocked": True,
        "vault_exp": int(time.time()) + 600,
    }
    probe_app.include_router(v_router)
    return probe_app


@pytest.mark.asyncio
async def test_api_guard_default_deny_stealth_404_and_cors_preflight():
    probe_app = _build_guard_probe_app()
    valid_user_tok = create_session_token("alice", role="user", vault_unlocked=False, vault_exp=0)
    expired_vault_tok = create_session_token(
        "alice",
        role="user",
        vault_unlocked=True,
        vault_exp=int(time.time()) - 10,
    )
    valid_vault_tok = create_session_token(
        "alice",
        role="user",
        vault_unlocked=True,
        vault_exp=int(time.time()) + 600,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=probe_app),
        base_url="http://test",
    ) as client:
        # Unprotected private route caught by default-deny middleware
        assert (await client.get("/api/forgotten")).status_code == 401
        assert (
            await client.get("/api/forgotten", cookies={settings.COOKIE_NAME: "tampered"})
        ).status_code == 401
        assert (
            await client.get("/api/forgotten", cookies={settings.COOKIE_NAME: valid_user_tok})
        ).status_code == 200
        assert (
            await client.get("/api/forgotten", headers={"Authorization": f"Bearer {valid_user_tok}"})
        ).status_code == 200

        # StreamingResponse works transparently through pure ASGI middleware
        stream_res = await client.get(
            "/api/stream-chunk",
            cookies={settings.COOKIE_NAME: valid_user_tok},
        )
        assert stream_res.status_code == 200
        assert stream_res.content == b"chunk-1:chunk-2"

        # Non-API pages bypass middleware
        assert (await client.get("/public-page")).status_code == 200

        # OPTIONS preflight passes middleware to outer CORSMiddleware
        pre = await client.options(
            "/api/forgotten",
            headers={
                "Origin": f"https://media.{settings.DOMAIN}",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert pre.status_code == 200
        assert pre.headers.get("access-control-allow-origin") == f"https://media.{settings.DOMAIN}"

        # /api/vault/x/* always returns 404 stealth without unlocked session (including no session)
        assert (await client.get("/api/vault/x/probe")).status_code == 404
        assert (
            await client.get("/api/vault/x/probe", cookies={settings.COOKIE_NAME: valid_user_tok})
        ).status_code == 404
        assert (
            await client.get("/api/vault/x/probe", cookies={settings.COOKIE_NAME: expired_vault_tok})
        ).status_code == 404

        ok_vault = await client.get(
            "/api/vault/x/probe",
            cookies={settings.COOKIE_NAME: valid_vault_tok},
        )
        assert ok_vault.status_code == 200
        assert ok_vault.json() == {"vault": "secret"}

