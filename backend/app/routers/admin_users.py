"""Admin-only user and role management endpoints (/api/admin/users)."""

from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_db
from backend.app.models import AuditLog, User, UserPasskey
from backend.app.routers._access import admin_router
from backend.app.security import require_admin, set_user_token_version

router = admin_router(prefix="/api/admin", tags=["Admin Users"])


class AdminUserItem(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    email_verified: bool
    passkey_count: int
    token_version: int
    created_at: str


class AdminUsersResponse(BaseModel):
    users: List[AdminUserItem]


class AdminUserUpdateRequest(BaseModel):
    role: Optional[Literal["admin", "user"]] = None
    is_active: Optional[bool] = None
    revoke_sessions: bool = False


def _serialize_user(user: User, passkey_count: int) -> AdminUserItem:
    return AdminUserItem(
        id=int(user.id),
        username=user.username,
        email=user.email,
        role=user.role or "user",
        is_active=bool(user.is_active),
        email_verified=user.email_verified_at is not None,
        passkey_count=int(passkey_count),
        token_version=int(user.token_version or 1),
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


@router.get("/users", response_model=AdminUsersResponse)
async def list_users(
    session: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return all user accounts and passkey counts without sensitive hashes or tokens."""
    users = (await db.execute(select(User).order_by(User.id.asc()))).scalars().all()
    pk_rows = (
        await db.execute(
            select(UserPasskey.user_id, func.count(UserPasskey.id)).group_by(UserPasskey.user_id)
        )
    ).all()
    pk_counts = {int(uid): int(cnt) for uid, cnt in pk_rows}
    return AdminUsersResponse(
        users=[_serialize_user(u, pk_counts.get(int(u.id), 0)) for u in users]
    )


@router.patch("/users/{user_id}", response_model=AdminUserItem)
async def update_user_admin(
    user_id: int,
    payload: AdminUserUpdateRequest,
    session: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's role/active state or increment token_version to revoke active sessions."""
    target = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    actor_username = str(session.get("sub") or "")
    is_self = target.username == actor_username
    if is_self and ((payload.role is not None and payload.role != "admin") or payload.is_active is False):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot demote or deactivate your own active admin account.",
        )

    next_role = payload.role if payload.role is not None else (target.role or "user")
    next_active = payload.is_active if payload.is_active is not None else bool(target.is_active)
    was_active_admin = bool(target.is_active) and (target.role == "admin")
    will_be_active_admin = bool(next_active) and (next_role == "admin")
    if was_active_admin and not will_be_active_admin:
        other_admins = (
            await db.execute(
                select(func.count(User.id)).where(
                    User.id != target.id,
                    User.role == "admin",
                    User.is_active.is_(True),
                )
            )
        ).scalar_one()
        if int(other_admins or 0) < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove or deactivate the last active administrator.",
            )

    changes: List[str] = []
    if payload.role is not None and payload.role != target.role:
        changes.append(f"role={target.role}->{payload.role}")
        target.role = payload.role
    if payload.is_active is not None and bool(payload.is_active) != bool(target.is_active):
        changes.append(f"active={bool(target.is_active)}->{bool(payload.is_active)}")
        target.is_active = bool(payload.is_active)

    if payload.revoke_sessions or (payload.role is not None) or (payload.is_active is False):
        target.token_version = int(target.token_version or 1) + 1
        set_user_token_version(target.username, target.token_version)
        changes.append(f"token_version={target.token_version}")

    if changes:
        db.add(
            AuditLog(
                user_id=target.id,
                username=actor_username or target.username,
                event="admin_user_updated",
                detail=f"target={target.username} " + " ".join(changes),
            )
        )
        await db.commit()
        await db.refresh(target)

    pk_count = (
        await db.execute(select(func.count(UserPasskey.id)).where(UserPasskey.user_id == target.id))
    ).scalar_one()
    return _serialize_user(target, int(pk_count or 0))

