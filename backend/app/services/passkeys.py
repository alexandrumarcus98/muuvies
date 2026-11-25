"""WebAuthn passkey service: single-use challenge store, RP/origin resolution, and verification."""

import json
import os
import secrets
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from backend.app.config import settings
from backend.app.models import AuditLog, User, UserPasskey
from backend.app.security import is_trusted_proxy_ip

_CHALLENGE_TTL_SECONDS = 300
_MAX_CHALLENGES = 2048
_challenges: Dict[str, Dict[str, Any]] = {}


class PasskeyVerificationError(ValueError):
    """Raised when a passkey attestation or assertion fails validation."""


def _prune_expired_challenges(now: Optional[float] = None) -> None:
    ts = now if now is not None else time.monotonic()
    expired = [cid for cid, entry in _challenges.items() if entry["expires_at"] <= ts]
    for cid in expired:
        _challenges.pop(cid, None)
    while len(_challenges) >= _MAX_CHALLENGES:
        oldest_key = next(iter(_challenges), None)
        if oldest_key is None:
            break
        _challenges.pop(oldest_key, None)


def issue_challenge(
    purpose: str,
    challenge_bytes: bytes,
    *,
    user_id: Optional[int] = None,
) -> str:
    """Store a cryptographic challenge under a single-use ID with a 5-minute TTL."""
    now = time.monotonic()
    _prune_expired_challenges(now)
    challenge_id = secrets.token_urlsafe(24)
    _challenges[challenge_id] = {
        "challenge": challenge_bytes,
        "purpose": purpose,
        "user_id": user_id,
        "expires_at": now + _CHALLENGE_TTL_SECONDS,
    }
    return challenge_id


def consume_challenge(
    challenge_id: str,
    *,
    expected_purpose: str,
    expected_user_id: Optional[int] = None,
) -> bytes:
    """Atomically pop and validate a challenge (single-use to prevent replay)."""
    key = (challenge_id or "").strip()
    entry = _challenges.pop(key, None)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey challenge is invalid, expired, or already used.",
        )
    if entry["expires_at"] <= time.monotonic():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey challenge has expired. Please try again.",
        )
    if entry["purpose"] != expected_purpose:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey challenge purpose mismatch.",
        )
    if expected_user_id is not None and entry.get("user_id") != expected_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey challenge does not belong to the active user.",
        )
    return entry["challenge"]


def clear_challenge_store() -> None:
    """Clear active WebAuthn challenges (intended for test isolation)."""
    _challenges.clear()


def resolve_rp_and_origins(request: Request) -> Tuple[str, List[str]]:
    """Resolve the effective RP ID and allowed origins for the incoming HTTP request."""
    peer_ip = request.client.host if request.client else ""
    fwd_host = request.headers.get("x-forwarded-host") if is_trusted_proxy_ip(peer_ip) else ""
    pub_base = (settings.PUBLIC_BASE_URL or "").strip()
    pub_netloc = ""
    if pub_base:
        parsed_pub_init = urlparse(pub_base if "://" in pub_base else f"https://{pub_base}")
        pub_netloc = parsed_pub_init.netloc or ""

    direct_host = (request.headers.get("host") or request.url.netloc or "").split(",")[0].strip()
    direct_name = direct_host.split(":")[0].strip().lower()
    if direct_name in {"localhost", "127.0.0.1", "test"}:
        raw_host = direct_host
    else:
        raw_host = (
            pub_netloc
            or (fwd_host or "").split(",")[0].strip()
            or direct_host
        )
    host_name = raw_host.split(":")[0].strip().lower()
    rp_id = settings.get_webauthn_rp_id(host_name)

    origins: List[str] = []

    def _add_origin(candidate: str) -> None:
        cleaned = (candidate or "").strip().rstrip("/")
        if cleaned and cleaned not in origins:
            origins.append(cleaned)

    is_dev_or_test = (
        rp_id in {"localhost", "127.0.0.1", "test"}
        or os.getenv("ENVIRONMENT", "").strip().lower() == "test"
    )

    req_origin = (request.headers.get("origin") or "").strip()
    if req_origin:
        try:
            parsed_origin = urlparse(req_origin)
            orig_host = (parsed_origin.hostname or "").lower()
            if orig_host == rp_id or orig_host.endswith(f".{rp_id}"):
                if parsed_origin.scheme == "https" or (is_dev_or_test and parsed_origin.scheme == "http"):
                    _add_origin(f"{parsed_origin.scheme}://{parsed_origin.netloc}")
        except Exception:
            pass

    pub_base = (settings.PUBLIC_BASE_URL or "").strip()
    if pub_base:
        try:
            parsed_pub = urlparse(pub_base if "://" in pub_base else f"https://{pub_base}")
            pub_host = (parsed_pub.hostname or "").lower()
            if pub_host == rp_id or pub_host.endswith(f".{rp_id}"):
                _add_origin(f"{parsed_pub.scheme}://{parsed_pub.netloc}")
        except Exception:
            pass

    _add_origin(f"https://{rp_id}")

    if is_dev_or_test:
        if raw_host and (host_name == rp_id or host_name.endswith(f".{rp_id}")):
            _add_origin(f"http://{raw_host}")
            _add_origin(f"https://{raw_host}")
        _add_origin(f"http://{rp_id}")
        base_str = str(request.base_url).rstrip("/")
        try:
            parsed_base = urlparse(base_str)
            b_host = (parsed_base.hostname or "").lower()
            if b_host == rp_id or b_host.endswith(f".{rp_id}"):
                _add_origin(base_str)
        except Exception:
            pass

    return rp_id, origins


def parse_transports(raw: Any) -> Optional[List[AuthenticatorTransport]]:
    """Safely parse WebAuthn transports; returns None when empty or unrecognized (1Password-safe)."""
    if not raw:
        return None
    items: Iterable[str]
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = (str(x) for x in raw)
    else:
        return None

    parsed: List[AuthenticatorTransport] = []
    for item in items:
        tok = str(item or "").strip().lower()
        if not tok:
            continue
        try:
            transport = AuthenticatorTransport(tok)
            if transport not in parsed:
                parsed.append(transport)
        except ValueError:
            continue
    return parsed or None


def serialize_transports(credential_payload: Dict[str, Any]) -> str:
    """Extract and normalize valid AuthenticatorTransport tokens into a comma-separated string."""
    raw = credential_payload.get("transports")
    if not raw and isinstance(credential_payload.get("response"), dict):
        raw = credential_payload["response"].get("transports")
    parsed = parse_transports(raw)
    if not parsed:
        return ""
    return ",".join(t.value for t in parsed)


def extract_credential_id(credential_payload: Dict[str, Any]) -> str:
    """Return normalized credential ID from a WebAuthn client payload."""
    cred_id = (
        credential_payload.get("id")
        or credential_payload.get("rawId")
        or ""
    )
    if not isinstance(cred_id, str) or not cred_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing WebAuthn credential identifier.",
        )
    return cred_id.strip().rstrip("=")


def _to_descriptors(passkeys: Iterable[UserPasskey]) -> List[PublicKeyCredentialDescriptor]:
    descriptors: List[PublicKeyCredentialDescriptor] = []
    for pk in passkeys:
        try:
            cred_bytes = base64url_to_bytes(pk.credential_id)
        except Exception:
            continue
        descriptors.append(
            PublicKeyCredentialDescriptor(
                id=cred_bytes,
                transports=parse_transports(pk.transports),
            )
        )
    return descriptors


def build_registration_options(
    request: Request,
    user: User,
    existing_passkeys: Iterable[UserPasskey],
) -> Tuple[str, Dict[str, Any]]:
    """Build WebAuthn registration options with 1Password / cross-platform compatibility."""
    rp_id, _ = resolve_rp_and_origins(request)
    display_name = f"{user.username} ({user.email})" if user.email else user.username
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=(settings.WEBAUTHN_RP_NAME or "Muuvies").strip(),
        user_id=f"muuvies-user-{user.id}".encode("utf-8"),
        user_name=user.username,
        user_display_name=display_name,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=_to_descriptors(existing_passkeys) or None,
    )
    challenge_id = issue_challenge("register", options.challenge, user_id=user.id)
    return challenge_id, json.loads(options_to_json(options))


def verify_registration_payload(
    request: Request,
    *,
    challenge_id: str,
    credential_payload: Dict[str, Any],
    user: User,
) -> Dict[str, Any]:
    """Consume the single-use challenge and verify WebAuthn registration attestation."""
    expected_challenge = consume_challenge(
        challenge_id,
        expected_purpose="register",
        expected_user_id=user.id,
    )
    rp_id, origins = resolve_rp_and_origins(request)
    try:
        verified = verify_registration_response(
            credential=credential_payload,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins,
            require_user_presence=True,
            require_user_verification=False,
        )
    except (WebAuthnException, ValueError, TypeError) as exc:
        raise PasskeyVerificationError(str(exc) or "Invalid passkey registration response.") from exc

    return {
        "credential_id": bytes_to_base64url(verified.credential_id),
        "public_key": bytes_to_base64url(verified.credential_public_key),
        "sign_count": int(verified.sign_count or 0),
        "transports": serialize_transports(credential_payload),
    }


def build_authentication_options(
    request: Request,
    *,
    purpose: str,
    user: Optional[User] = None,
    allow_passkeys: Optional[Iterable[UserPasskey]] = None,
    user_verification: UserVerificationRequirement = UserVerificationRequirement.PREFERRED,
) -> Tuple[str, Dict[str, Any]]:
    """Build WebAuthn authentication options for discoverable login or scoped Vault unlock."""
    rp_id, _ = resolve_rp_and_origins(request)
    descriptors = _to_descriptors(allow_passkeys) if allow_passkeys is not None else None
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=descriptors or None,
        user_verification=user_verification,
    )
    challenge_id = issue_challenge(
        purpose,
        options.challenge,
        user_id=user.id if user else None,
    )
    return challenge_id, json.loads(options_to_json(options))


def verify_authentication_payload(
    request: Request,
    *,
    challenge_id: str,
    expected_purpose: str,
    credential_payload: Dict[str, Any],
    passkey: UserPasskey,
    expected_user_id: Optional[int] = None,
    require_user_verification: bool = False,
) -> int:
    """Consume single-use challenge and verify assertion against a stored UserPasskey."""
    expected_challenge = consume_challenge(
        challenge_id,
        expected_purpose=expected_purpose,
        expected_user_id=expected_user_id,
    )
    rp_id, origins = resolve_rp_and_origins(request)
    try:
        verified = verify_authentication_response(
            credential=credential_payload,
            expected_challenge=expected_challenge,
            expected_rp_id=rp_id,
            expected_origin=origins,
            credential_public_key=base64url_to_bytes(passkey.public_key),
            credential_current_sign_count=int(passkey.sign_count or 0),
            require_user_verification=require_user_verification,
        )
    except (WebAuthnException, ValueError, TypeError) as exc:
        raise PasskeyVerificationError(str(exc) or "Passkey verification failed.") from exc

    return int(verified.new_sign_count or 0)


async def record_audit_event(
    db: AsyncSession,
    *,
    event: str,
    user: Optional[User] = None,
    detail: str = "",
    ip_address: Optional[str] = None,
) -> None:
    """Append an audit log entry inside the current DB session."""
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            event=event,
            detail=detail,
            ip_address=ip_address,
        )
    )
