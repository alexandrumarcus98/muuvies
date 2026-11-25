import hashlib
import hmac
import re
import secrets
from datetime import timedelta, timezone
from typing import Union
from urllib.parse import urlparse
from fastapi import Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import ORJSONResponse, RedirectResponse
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import User, UserPasskey, utcnow
from backend.app.routers._access import public_router, user_router
from backend.app.schemas import (
    AuthConfigResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    PasskeyAuthVerifyRequest,
    PasskeyInfo,
    PasskeyListResponse,
    PasskeyOptionsResponse,
    PasskeyRegisterVerifyRequest,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    UserResponse,
    VerificationSentResponse,
    VerifyEmailRequest,
)
from backend.app.security import (
    create_session_token,
    decode_session_token,
    extract_session_token,
    get_client_ip,
    get_current_session,
    is_trusted_proxy_ip,
    login_limiter,
    register_limiter,
    resend_limiter,
    set_user_token_version,
    verify_secret,
    hash_secret,
    check_vault_unlocked,
)
from backend.app.services.email_service import send_password_reset_email, send_verification_email
from backend.app.services.passkeys import (
    PasskeyVerificationError,
    build_authentication_options,
    build_registration_options,
    consume_challenge,
    extract_credential_id,
    record_audit_event,
    verify_authentication_payload,
    verify_registration_payload,
)
from webauthn.helpers.structs import UserVerificationRequirement

router = public_router(prefix="/api/auth", tags=["Authentication"])
verify_router = public_router(tags=["Authentication"])
_user_router = user_router(tags=["Authentication"])

_VERIFICATION_TTL_HOURS = 24
_RESET_TTL_MINUTES = 30


def _hash_verification_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _set_verification_token(user: User) -> str:
    raw_token = secrets.token_urlsafe(32)
    user.verification_token_hash = _hash_verification_token(raw_token)
    user.verification_expires_at = utcnow() + timedelta(hours=_VERIFICATION_TTL_HOURS)
    return raw_token


def _set_reset_token(user: User) -> str:
    raw_token = secrets.token_urlsafe(32)
    user.reset_token_hash = _hash_verification_token(raw_token)
    user.reset_expires_at = utcnow() + timedelta(minutes=_RESET_TTL_MINUTES)
    return raw_token


def _build_action_url(request: Request, path: str, raw_token: str) -> str:
    base_url = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base_url:
        req_base = str(request.base_url).rstrip("/")
        parsed = urlparse(req_base)
        req_host = (parsed.hostname or "").lower()
        domain = (settings.DOMAIN or "").strip().lower()
        allowed_hosts = {"localhost", "127.0.0.1", "test"}
        if domain:
            allowed_hosts.update({domain, f"media.{domain}"})
        peer_ip = request.client.host if request.client else ""
        if req_host in {"localhost", "127.0.0.1", "test"} or (
            is_trusted_proxy_ip(peer_ip) and req_host in allowed_hosts
        ):
            base_url = req_base
        elif domain:
            base_url = f"https://media.{domain}"
        else:
            base_url = req_base
    return f"{base_url}{path}?token={raw_token}"


def _build_verify_url(request: Request, raw_token: str) -> str:
    return _build_action_url(request, "/verify-email", raw_token)


def _issue_session_cookie(response: Response, user: User) -> None:
    token_ver = int(getattr(user, "token_version", 1) or 1)
    set_user_token_version(user.username, token_ver)
    token = create_session_token(
        user.username,
        email=user.email or "",
        role=user.role or "user",
        vault_unlocked=False,
        ver=token_ver,
    )
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
        max_age=settings.SESSION_EXPIRE_HOURS * 3600,
        path="/",
    )


async def _verify_user_by_token(raw_token: str, db: AsyncSession) -> User:
    token_str = (raw_token or "").strip()
    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    token_hash = _hash_verification_token(token_str)
    stmt = select(User).where(User.verification_token_hash == token_hash)
    user = (await db.execute(stmt)).scalars().first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification link.",
        )

    expires_at = user.verification_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or expires_at < utcnow():
        user.verification_token_hash = None
        user.verification_expires_at = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification link has expired. Please request a new one.",
        )

    user.email_verified_at = utcnow()
    user.verification_token_hash = None
    user.verification_expires_at = None
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config():
    """Return public authentication capabilities for the login/register page."""
    return AuthConfigResponse(
        allow_registration=bool(settings.ALLOW_REGISTRATION),
        invite_required=bool((settings.REGISTRATION_INVITE_CODE or "").strip()),
        vault_timeout_minutes=int(settings.VAULT_TIMEOUT_MINUTES),
        require_email_verification=bool(settings.REQUIRE_EMAIL_VERIFICATION),
    )


@router.post("/register", response_model=Union[LoginResponse, VerificationSentResponse])
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled on this node.",
        )

    ip = get_client_ip(request)
    is_limited, retry_after = register_limiter.is_rate_limited(ip)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many registration attempts. Please wait {retry_after} seconds.",
        )

    expected_invite = (settings.REGISTRATION_INVITE_CODE or "").strip()
    if expected_invite:
        provided_invite = (payload.invite_code or "").strip()
        if not provided_invite or not hmac.compare_digest(provided_invite, expected_invite):
            register_limiter.record_failure(ip)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or missing registration invite code.",
            )
    # Validate username
    uname = payload.username.strip()
    if len(uname) < 3 or not re.match(r"^[a-zA-Z0-9_-]+$", uname):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username must be at least 3 characters and contain only letters, numbers, hyphens, and underscores.",
        )

    # Validate email
    email = payload.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email address format.",
        )

    allowed_emails = settings.get_allowed_emails_set()
    if allowed_emails and email not in allowed_emails:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is restricted to approved email addresses.",
        )

    # Validate password length
    if len(payload.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    # Check for existing username (case-insensitive) or email
    stmt = select(User).where(or_(func.lower(User.username) == uname.lower(), User.email == email))
    existing = (await db.execute(stmt)).scalars().first()
    if existing:
        if existing.username.lower() == uname.lower():
            detail = "Username is already registered."
        else:
            detail = "Email is already registered."
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    # First user bootstrap: First registered user automatically becomes admin
    count_stmt = select(func.count(User.id))
    user_count = (await db.execute(count_stmt)).scalar() or 0
    role = "admin" if user_count == 0 else "user"

    # Hash password with Argon2id
    pw_hash = hash_secret(payload.password)
    require_verify = bool(settings.REQUIRE_EMAIL_VERIFICATION)

    new_user = User(
        username=uname,
        email=email,
        password_hash=pw_hash,
        role=role,
        is_active=True,
        vault_pin_hash="",
        email_verified_at=None if require_verify else utcnow(),
    )

    if require_verify:
        raw_token = _set_verification_token(new_user)
        db.add(new_user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username or email is already registered.",
            )
        await db.refresh(new_user)

        verify_url = _build_verify_url(request, raw_token)
        await send_verification_email(new_user.email, verify_url)

        return VerificationSentResponse(
            status="verification_sent",
            email=new_user.email,
            message="Verification email sent. Please check your inbox to activate your account.",
        )

    db.add(new_user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email is already registered.",
        )
    await db.refresh(new_user)

    # Issue signed session token
    _issue_session_cookie(response, new_user)

    return LoginResponse(
        success=True,
        username=new_user.username,
        email=new_user.email,
        role=new_user.role,
        vault_unlocked=False,
        message=f"Account created successfully with role '{new_user.role}'",
    )


@verify_router.get("/verify-email")
async def verify_email_redirect(
    token: str = Query(""),
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await _verify_user_by_token(token, db)
    except HTTPException:
        return RedirectResponse(url="/login?verify_error=1", status_code=status.HTTP_302_FOUND)

    redirect = RedirectResponse(url="/login?verified=1", status_code=status.HTTP_302_FOUND)
    _issue_session_cookie(redirect, user)
    return redirect


@router.post("/verify-email", response_model=LoginResponse)
async def verify_email_api(
    payload: VerifyEmailRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    user = await _verify_user_by_token(payload.token, db)
    _issue_session_cookie(response, user)
    return LoginResponse(
        success=True,
        username=user.username,
        email=user.email,
        role=user.role or "user",
        vault_unlocked=False,
        message="Email verified successfully.",
    )


@router.post("/resend-verification")
async def resend_verification(
    payload: ResendVerificationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = get_client_ip(request)
    email = payload.email.strip().lower()
    rate_key = f"{ip}:{email}"
    is_limited, retry_after = resend_limiter.is_rate_limited(rate_key)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {retry_after} seconds before requesting another verification email.",
        )

    resend_limiter.record_failure(rate_key)

    stmt = select(User).where(User.email == email)
    user = (await db.execute(stmt)).scalars().first()
    if user and user.is_active and user.email_verified_at is None:
        raw_token = _set_verification_token(user)
        await db.commit()
        verify_url = _build_verify_url(request, raw_token)
        await send_verification_email(user.email, verify_url)

    return {
        "status": "ok",
        "message": "If an unverified account matches that email, a verification link has been sent.",
    }


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ip = get_client_ip(request)
    email = payload.email.strip().lower()
    rate_key = f"reset:{ip}:{email}"
    is_limited, retry_after = resend_limiter.is_rate_limited(rate_key)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Please wait {retry_after} seconds before requesting another reset email.",
        )

    resend_limiter.record_failure(rate_key)

    stmt = select(User).where(User.email == email)
    user = (await db.execute(stmt)).scalars().first()
    if user and user.is_active:
        raw_token = _set_reset_token(user)
        await db.commit()
        reset_url = _build_action_url(request, "/reset-password", raw_token)
        await send_password_reset_email(user.email, reset_url)

    return {
        "status": "ok",
        "message": "If an account matches that email, a password reset link has been sent.",
    }


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    raw_token = (payload.token or "").strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset link.",
        )
    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters long.",
        )

    token_hash = _hash_verification_token(raw_token)
    stmt = select(User).where(User.reset_token_hash == token_hash)
    user = (await db.execute(stmt)).scalars().first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset link.",
        )

    expires_at = user.reset_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or expires_at < utcnow():
        user.reset_token_hash = None
        user.reset_expires_at = None
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset link has expired. Please request a new one.",
        )

    next_version = int(getattr(user, "token_version", 1) or 1) + 1
    user.password_hash = hash_secret(payload.new_password)
    user.token_version = next_version
    user.reset_token_hash = None
    user.reset_expires_at = None
    await db.commit()

    set_user_token_version(user.username, next_version)
    response.delete_cookie(
        key=settings.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
    )
    return {
        "success": True,
        "message": "Password reset successfully. Please sign in with your new password.",
    }


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    ip = get_client_ip(request)
    is_limited, retry_after = login_limiter.is_rate_limited(ip)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please wait {retry_after} seconds before trying again.",
        )

    login_ident = payload.username.strip().lower()

    # Query strictly against DB by username (case-insensitive) OR email
    stmt = select(User).where(or_(func.lower(User.username) == login_ident, User.email == login_ident))
    res = await db.execute(stmt)
    user = res.scalars().first()

    authenticated = False
    if user and verify_secret(payload.password, user.password_hash):
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is suspended. Please contact the administrator.",
            )
        authenticated = True

    if not authenticated:
        login_limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password",
        )

    if settings.REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None:
        login_limiter.reset(ip)
        return ORJSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Email not verified", "code": "EMAIL_UNVERIFIED"},
        )

    # Login succeeded, reset limiter
    login_limiter.reset(ip)

    # Issue signed session token with role & email
    _issue_session_cookie(response, user)

    return LoginResponse(
        success=True,
        username=user.username,
        email=user.email,
        role=user.role or "user",
        vault_unlocked=False,
        message="Login successful",
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    token = extract_session_token(request)
    if token:
        payload = decode_session_token(token)
        username = (payload.get("sub") or "").strip() if payload else ""
        if username:
            user = (await db.execute(select(User).where(User.username == username))).scalars().first()
            if user:
                next_ver = int(getattr(user, "token_version", 1) or 1) + 1
                user.token_version = next_ver
                await db.commit()
                set_user_token_version(user.username, next_ver)

    response.delete_cookie(
        key=settings.COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
        secure=settings.COOKIE_SECURE,
    )
    return {"success": True, "message": "Logged out successfully"}


@_user_router.get("/me")
async def get_me(
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    await _require_active_db_user(session, db)
    vault_active = check_vault_unlocked(session)
    return {
        "authenticated": True,
        "username": session.get("sub", ""),
        "email": session.get("email", ""),
        "role": session.get("role", "user"),
        "vault_unlocked": vault_active,
    }


def _to_passkey_info(pk: UserPasskey) -> PasskeyInfo:
    transports = [t for t in (pk.transports or "").split(",") if t.strip()]
    return PasskeyInfo(
        id=pk.id,
        device_name=pk.device_name or "Passkey",
        transports=transports,
        sign_count=int(pk.sign_count or 0),
        created_at=pk.created_at,
        last_used_at=pk.last_used_at,
    )


async def _require_active_db_user(session: dict, db: AsyncSession) -> User:
    username = (session.get("sub") or "").strip()
    user = (await db.execute(select(User).where(User.username == username))).scalars().first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is inactive or unavailable.",
        )
    token_ver = int(session.get("ver", 1) or 1)
    db_ver = int(getattr(user, "token_version", 1) or 1)
    set_user_token_version(user.username, db_ver)
    if token_ver < db_ver:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session revoked. Please sign in again.",
        )
    return user


@_user_router.get("/passkeys", response_model=PasskeyListResponse)
async def list_passkeys(
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """List the authenticated user's registered passkeys."""
    user = await _require_active_db_user(session, db)
    rows = (
        await db.execute(
            select(UserPasskey)
            .where(UserPasskey.user_id == user.id)
            .order_by(UserPasskey.created_at.desc())
        )
    ).scalars().all()
    return PasskeyListResponse(passkeys=[_to_passkey_info(pk) for pk in rows])


@_user_router.post("/passkeys/register/options", response_model=PasskeyOptionsResponse)
async def passkey_register_options(
    request: Request,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Generate WebAuthn registration options excluding already-registered credentials."""
    user = await _require_active_db_user(session, db)
    existing = (
        await db.execute(select(UserPasskey).where(UserPasskey.user_id == user.id))
    ).scalars().all()
    challenge_id, options = build_registration_options(request, user, existing)
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@_user_router.post("/passkeys/register/verify", response_model=PasskeyInfo)
async def passkey_register_verify(
    payload: PasskeyRegisterVerifyRequest,
    request: Request,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Verify WebAuthn registration attestation and persist UserPasskey + AuditLog."""
    user = await _require_active_db_user(session, db)
    try:
        verified = verify_registration_payload(
            request,
            challenge_id=payload.challenge_id,
            credential_payload=payload.credential,
            user=user,
        )
    except PasskeyVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Passkey registration verification failed: {exc}",
        ) from exc

    duplicate = (
        await db.execute(
            select(UserPasskey).where(UserPasskey.credential_id == verified["credential_id"])
        )
    ).scalars().first()
    if duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This passkey is already registered.",
        )

    device_name = (payload.device_name or "").strip()[:128] or "Passkey"
    passkey = UserPasskey(
        user_id=user.id,
        credential_id=verified["credential_id"],
        public_key=verified["public_key"],
        sign_count=verified["sign_count"],
        transports=verified["transports"],
        device_name=device_name,
    )
    db.add(passkey)
    await record_audit_event(
        db,
        event="passkey_registered",
        user=user,
        detail=f"Registered passkey '{device_name}'",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    await db.refresh(passkey)
    return _to_passkey_info(passkey)


@_user_router.delete("/passkeys/{passkey_id}")
async def delete_passkey(
    passkey_id: int,
    request: Request,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Revoke one of the authenticated user's registered passkeys."""
    user = await _require_active_db_user(session, db)
    passkey = (
        await db.execute(
            select(UserPasskey).where(
                UserPasskey.id == passkey_id,
                UserPasskey.user_id == user.id,
            )
        )
    ).scalars().first()
    if not passkey:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Passkey not found.",
        )
    dev_name = passkey.device_name
    await db.delete(passkey)
    await record_audit_event(
        db,
        event="passkey_deleted",
        user=user,
        detail=f"Revoked passkey '{dev_name}'",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"success": True, "deleted_id": passkey_id}


@router.post("/passkeys/login/options", response_model=PasskeyOptionsResponse)
async def passkey_login_options(request: Request):
    """Generate discoverable WebAuthn authentication options for passwordless sign-in."""
    ip = get_client_ip(request)
    is_limited, retry_after = login_limiter.is_rate_limited(ip)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please wait {retry_after} seconds before trying again.",
        )
    challenge_id, options = build_authentication_options(
        request,
        purpose="login",
        user=None,
        allow_passkeys=None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return PasskeyOptionsResponse(challenge_id=challenge_id, options=options)


@router.post("/passkeys/login/verify", response_model=LoginResponse)
async def passkey_login_verify(
    payload: PasskeyAuthVerifyRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Verify WebAuthn assertion and issue a standard HttpOnly session cookie."""
    ip = get_client_ip(request)
    is_limited, retry_after = login_limiter.is_rate_limited(ip)
    if is_limited:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please wait {retry_after} seconds before trying again.",
        )

    cred_id = extract_credential_id(payload.credential)
    passkey = (
        await db.execute(select(UserPasskey).where(UserPasskey.credential_id == cred_id))
    ).scalars().first()
    if not passkey:
        try:
            consume_challenge(payload.challenge_id, expected_purpose="login")
        except HTTPException:
            pass
        login_limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Passkey is not recognized on this node.",
        )

    user = (await db.execute(select(User).where(User.id == passkey.user_id))).scalars().first()
    try:
        new_sign_count = verify_authentication_payload(
            request,
            challenge_id=payload.challenge_id,
            expected_purpose="login",
            credential_payload=payload.credential,
            passkey=passkey,
            require_user_verification=False,
        )
    except PasskeyVerificationError as exc:
        login_limiter.record_failure(ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Passkey sign-in verification failed.",
        ) from exc

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is suspended. Please contact the administrator.",
        )

    if settings.REQUIRE_EMAIL_VERIFICATION and user.email_verified_at is None:
        login_limiter.reset(ip)
        return ORJSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "Email not verified", "code": "EMAIL_UNVERIFIED"},
        )

    passkey.sign_count = new_sign_count
    passkey.last_used_at = utcnow()
    await record_audit_event(
        db,
        event="passkey_login",
        user=user,
        detail=f"Signed in via passkey '{passkey.device_name}'",
        ip_address=ip,
    )
    await db.commit()

    login_limiter.reset(ip)
    _issue_session_cookie(response, user)

    return LoginResponse(
        success=True,
        username=user.username,
        email=user.email,
        role=user.role or "user",
        vault_unlocked=False,
        message="Signed in with passkey",
    )


router.include_router(_user_router)

