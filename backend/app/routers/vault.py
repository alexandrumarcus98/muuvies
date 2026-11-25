import hmac
import time
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.structs import UserVerificationRequirement

from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import User, UserPasskey, utcnow
from backend.app.routers._access import public_router, user_router
from backend.app.schemas import (
    PasskeyAuthVerifyRequest,
    PasskeyOptionsResponse,
    VaultStatusResponse,
    VaultUnlockRequest,
)
from backend.app.security import (
    create_session_token,
    get_client_ip,
    get_current_session,
    get_current_user_optional,
    vault_limiter,
    verify_secret,
    check_vault_unlocked,
)
from backend.app.services.passkeys import (
    PasskeyVerificationError,
    build_authentication_options,
    consume_challenge,
    extract_credential_id,
    record_audit_event,
    verify_authentication_payload,
)

router = public_router(prefix="/api/vault", tags=["Adult Vault"])
_user_router = user_router(tags=["Adult Vault"])


def _issue_vault_unlocked_response(response: Response, session: dict) -> VaultStatusResponse:
    """Issue a refreshed HttpOnly session cookie with Vault unlocked for configured timeout."""
    now = int(time.time())
    vault_exp = now + (settings.VAULT_TIMEOUT_MINUTES * 60)
    new_token = create_session_token(
        session.get("sub", ""),
        email=session.get("email", ""),
        role=session.get("role", "user"),
        vault_unlocked=True,
        vault_exp=vault_exp,
        ver=int(session.get("ver", 1) or 1),
    )

    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=new_token,
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
        max_age=settings.SESSION_EXPIRE_HOURS * 3600,
        path="/",
    )

    return VaultStatusResponse(
        unlocked=True,
        expires_in_seconds=settings.VAULT_TIMEOUT_MINUTES * 60,
        message="Adult Vault unlocked successfully. Stealth mode disengaged.",
    )


async def _require_stealth_vault_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[dict, User]:
    """Return active Vault-eligible user or raise 404 Not Found to preserve Vault stealth."""
    session = await get_current_user_optional(request)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    username = (session.get("sub") or "").strip()
    user = (await db.execute(select(User).where(User.username == username))).scalars().first()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    token_ver = int(session.get("ver", 1) or 1)
    if token_ver < int(getattr(user, "token_version", 1) or 1):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    has_vault_access = user.role == "admin" or bool(user.vault_pin_hash)
    if not has_vault_access:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return session, user


@_user_router.post("/unlock", response_model=VaultStatusResponse)
async def unlock_vault(
    payload: VaultUnlockRequest,
    request: Request,
    response: Response,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    username = session.get("sub", "")
    ip = get_client_ip(request)
    rate_key = f"{ip}:{username}"
    is_limited, retry_after = vault_limiter.is_rate_limited(rate_key)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed Vault PIN attempts. Please wait {retry_after} seconds.",
        )

    # Check pin in DB (never let standard users bypass via global admin fallback PIN)
    stmt = select(User).where(User.username == username)
    res = await db.execute(stmt)
    user = res.scalars().first()

    pin_valid = False
    if user and user.vault_pin_hash:
        pin_valid = verify_secret(payload.pin, user.vault_pin_hash)
    elif (user and user.role == "admin") or (not user and session.get("role") == "admin"):
        if settings.VAULT_PIN and hmac.compare_digest(payload.pin, settings.VAULT_PIN):
            pin_valid = True

    if not pin_valid:
        vault_limiter.record_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Incorrect Adult Vault PIN",
        )

    vault_limiter.reset(rate_key)
    return _issue_vault_unlocked_response(response, session)


@router.post("/unlock/passkey/options", response_model=PasskeyOptionsResponse)
async def vault_unlock_passkey_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    vault_auth: tuple[dict, User] = Depends(_require_stealth_vault_user),
):
    """Generate biometric WebAuthn options scoped strictly to the authenticated Vault user."""
    session, user = vault_auth
    ip = get_client_ip(request)
    rate_key = f"{ip}:{user.username}"
    is_limited, retry_after = vault_limiter.is_rate_limited(rate_key)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed Vault attempts. Please wait {retry_after} seconds.",
        )

    passkeys = (
        await db.execute(select(UserPasskey).where(UserPasskey.user_id == user.id))
    ).scalars().all()
    if not passkeys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No passkeys registered for this account.",
        )

    challenge_id, options = build_authentication_options(
        request,
        purpose="vault",
        user=user,
        allow_passkeys=passkeys,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@router.post("/unlock/passkey/verify", response_model=VaultStatusResponse)
async def vault_unlock_passkey_verify(
    payload: PasskeyAuthVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    vault_auth: tuple[dict, User] = Depends(_require_stealth_vault_user),
):
    """Verify biometric WebAuthn assertion (UV required) and unlock the Adult Vault session."""
    session, user = vault_auth
    ip = get_client_ip(request)
    rate_key = f"{ip}:{user.username}"
    is_limited, retry_after = vault_limiter.is_rate_limited(rate_key)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed Vault attempts. Please wait {retry_after} seconds.",
        )

    cred_id = extract_credential_id(payload.credential)
    passkey = (
        await db.execute(
            select(UserPasskey).where(
                UserPasskey.credential_id == cred_id,
                UserPasskey.user_id == user.id,
            )
        )
    ).scalars().first()
    if not passkey:
        try:
            consume_challenge(
                payload.challenge_id,
                expected_purpose="vault",
                expected_user_id=user.id,
            )
        except HTTPException:
            pass
        vault_limiter.record_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Passkey is not registered to this account.",
        )

    try:
        new_sign_count = verify_authentication_payload(
            request,
            challenge_id=payload.challenge_id,
            expected_purpose="vault",
            credential_payload=payload.credential,
            passkey=passkey,
            expected_user_id=user.id,
            require_user_verification=True,
        )
    except PasskeyVerificationError as exc:
        vault_limiter.record_failure(rate_key)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Biometric passkey verification failed.",
        ) from exc

    passkey.sign_count = new_sign_count
    passkey.last_used_at = utcnow()
    await record_audit_event(
        db,
        event="vault_unlock_passkey",
        user=user,
        detail=f"Unlocked Adult Vault via passkey '{passkey.device_name}'",
        ip_address=ip,
    )
    await db.commit()

    vault_limiter.reset(rate_key)
    return _issue_vault_unlocked_response(response, session)


@_user_router.post("/lock", response_model=VaultStatusResponse)
async def lock_vault(
    response: Response,
    session: dict = Depends(get_current_session),
):
    username = session.get("sub", "")
    new_token = create_session_token(
        username,
        email=session.get("email", ""),
        role=session.get("role", "user"),
        vault_unlocked=False,
        vault_exp=0,
        ver=int(session.get("ver", 1) or 1),
    )

    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=new_token,
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
        max_age=settings.SESSION_EXPIRE_HOURS * 3600,
        path="/",
    )

    return VaultStatusResponse(
        unlocked=False,
        expires_in_seconds=0,
        message="Adult Vault locked. Stealth mode engaged.",
    )


@_user_router.get("/status", response_model=VaultStatusResponse)
async def vault_status(session: dict = Depends(get_current_session)):
    unlocked = check_vault_unlocked(session)
    remaining = 0
    if unlocked:
        remaining = max(0, session.get("vault_exp", 0) - int(time.time()))
    return VaultStatusResponse(
        unlocked=unlocked,
        expires_in_seconds=remaining,
        message="Vault is unlocked" if unlocked else "Vault is locked (Stealth Mode)",
    )


router.include_router(_user_router)

