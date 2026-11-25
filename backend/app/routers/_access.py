"""Access-tiered FastAPI router factories enforcing explicit default dependencies."""

from __future__ import annotations

from typing import Optional, Sequence

from fastapi import APIRouter, Depends

from backend.app.security import (
    get_current_session,
    require_admin,
    require_vault_unlocked,
)


def _tags_list(tags: Optional[Sequence[str]]) -> Optional[list]:
    return list(tags) if tags is not None else None


def public_router(
    *,
    prefix: str = "",
    tags: Optional[Sequence[str]] = None,
) -> APIRouter:
    """Router for deliberately public or token/stealth self-gated routes."""
    return APIRouter(prefix=prefix, tags=_tags_list(tags))


def user_router(
    *,
    prefix: str = "",
    tags: Optional[Sequence[str]] = None,
) -> APIRouter:
    """Router requiring a validated authenticated user session."""
    return APIRouter(
        prefix=prefix,
        tags=_tags_list(tags),
        dependencies=[Depends(get_current_session)],
    )


def admin_router(
    *,
    prefix: str = "",
    tags: Optional[Sequence[str]] = None,
) -> APIRouter:
    """Router requiring an authenticated administrator session."""
    return APIRouter(
        prefix=prefix,
        tags=_tags_list(tags),
        dependencies=[Depends(require_admin)],
    )


def vault_router(
    *,
    prefix: str = "",
    tags: Optional[Sequence[str]] = None,
) -> APIRouter:
    """Router requiring both an active session and an unlocked Adult Vault (404 stealth when locked)."""
    return APIRouter(
        prefix=prefix,
        tags=_tags_list(tags),
        dependencies=[Depends(get_current_session), Depends(require_vault_unlocked)],
    )

