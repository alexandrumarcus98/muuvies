import ipaddress
import json
import time
from typing import Optional, Dict, Tuple

from argon2 import PasswordHasher
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, HTTPException, status, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, get_db
from backend.app.models import User

# Argon2 Password Hasher
_hasher = PasswordHasher()


def hash_secret(secret: str) -> str:
    """Hashes a password or PIN using Argon2id."""
    return _hasher.hash(secret)


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Verifies a plain secret against an Argon2id hash."""
    try:
        return _hasher.verify(secret_hash, secret)
    except Exception:
        return False


# Fernet Symmetric Encryption for Settings & Session Tokens
def get_fernet() -> Fernet:
    return Fernet(settings.get_fernet_key())


def encrypt_text(plain_text: str) -> str:
    f = get_fernet()
    return f.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_text(cipher_text: str) -> str:
    f = get_fernet()
    return f.decrypt(cipher_text.encode("utf-8")).decode("utf-8")


# Session Token Management (Signed + Authenticated via Fernet)
_user_token_version_floors: Dict[str, int] = {}


def set_user_token_version(username: str, version: int) -> None:
    """Record minimum valid token version for a user in memory."""
    if username:
        _user_token_version_floors[username] = max(1, int(version))


def create_session_token(
    username: str,
    email: str = "",
    role: str = "user",
    vault_unlocked: bool = False,
    vault_exp: Optional[int] = None,
    ver: int = 1,
) -> str:
    now = int(time.time())
    exp = now + (settings.SESSION_EXPIRE_HOURS * 3600)
    payload = {
        "sub": username,
        "email": email,
        "role": role,
        "ver": int(ver),
        "iat": now,
        "exp": exp,
        "vault_unlocked": vault_unlocked,
        "vault_exp": vault_exp if vault_unlocked else 0,
    }
    return encrypt_text(json.dumps(payload))


def decode_session_token(token: str) -> Optional[dict]:
    try:
        data_str = decrypt_text(token)
        payload = json.loads(data_str)
        now = int(time.time())
        if payload.get("exp", 0) < now:
            return None
        username = payload.get("sub", "")
        min_ver = _user_token_version_floors.get(username)
        if min_ver is not None and int(payload.get("ver", 1)) < min_ver:
            return None
        # Check vault expiration
        if payload.get("vault_unlocked"):
            if payload.get("vault_exp", 0) < now:
                payload["vault_unlocked"] = False
                payload["vault_exp"] = 0
        return payload
    except (InvalidToken, Exception):
        return None


# Login Rate Limiting (In-Memory Sliding Window with Bounded Key Table)
class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300, max_keys: int = 4096):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_keys = max(1, int(max_keys))
        self.attempts: Dict[str, list] = {}

    def _prune_stale(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale_keys = []
        for key, stamps in self.attempts.items():
            fresh = [t for t in stamps if t > cutoff]
            if fresh:
                self.attempts[key] = fresh
            else:
                stale_keys.append(key)
        for key in stale_keys:
            self.attempts.pop(key, None)

    def is_rate_limited(self, ip: str) -> Tuple[bool, int]:
        now = time.time()
        stamps = self.attempts.get(ip)
        if not stamps:
            return False, 0
        fresh = [t for t in stamps if now - t < self.window_seconds]
        if not fresh:
            self.attempts.pop(ip, None)
            return False, 0
        self.attempts[ip] = fresh
        if len(fresh) >= self.max_attempts:
            retry_after = int(self.window_seconds - (now - fresh[0]))
            return True, max(1, retry_after)
        return False, 0

    def record_failure(self, ip: str):
        now = time.time()
        existing = self.attempts.get(ip)
        fresh = [t for t in existing if now - t < self.window_seconds] if existing else []
        if not fresh and ip not in self.attempts and len(self.attempts) >= self.max_keys:
            self._prune_stale(now)
            while len(self.attempts) >= self.max_keys:
                oldest_key = next(iter(self.attempts), None)
                if oldest_key is None:
                    break
                self.attempts.pop(oldest_key, None)
        fresh.append(now)
        self.attempts.pop(ip, None)
        self.attempts[ip] = fresh

    def reset(self, ip: str):
        self.attempts.pop(ip, None)


login_limiter = LoginRateLimiter()
register_limiter = LoginRateLimiter(max_attempts=5, window_seconds=600)
vault_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300)
resend_limiter = LoginRateLimiter(max_attempts=1, window_seconds=120)

_TRUSTED_PROXY_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("172.16.0.0/12"),
)


def is_trusted_proxy_ip(ip_str: Optional[str]) -> bool:
    """Return True only for loopback and Docker bridge proxies (never WireGuard 10.0.0.0/8 or public peers)."""
    raw = (ip_str or "").strip()
    if not raw:
        return False
    try:
        addr = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _TRUSTED_PROXY_NETS)


def _parse_valid_ip(candidate: str) -> Optional[str]:
    raw = (candidate or "").strip()
    if not raw:
        return None
    try:
        ipaddress.ip_address(raw.split("%", 1)[0])
        return raw
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    peer_ip = (request.client.host if request.client else "") or "127.0.0.1"
    if not is_trusted_proxy_ip(peer_ip):
        return peer_ip

    real_ip = _parse_valid_ip(request.headers.get("X-Real-IP") or "")
    if real_ip:
        return real_ip

    forwarded = request.headers.get("X-Forwarded-For") or ""
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        valid_hops = [ip for ip in (_parse_valid_ip(h) for h in hops) if ip is not None]
        for hop in reversed(valid_hops):
            if not is_trusted_proxy_ip(hop):
                return hop
        if valid_hops:
            return valid_hops[0]

    return peer_ip


def extract_session_token(request: Request) -> Optional[str]:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
    return token or None


async def _validate_session_against_db(
    payload: Optional[dict],
    db: Optional[AsyncSession] = None,
) -> Optional[dict]:
    if not payload:
        return None
    username = (payload.get("sub") or "").strip()
    if not username:
        return None

    stmt = select(User.is_active, User.token_version, User.role).where(User.username == username)
    if db is not None:
        row = (await db.execute(stmt)).first()
    else:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(stmt)).first()

    if not row:
        return None
    is_active, db_token_version, db_role = row
    if not is_active:
        return None

    db_ver = int(db_token_version or 1)
    set_user_token_version(username, db_ver)
    if int(payload.get("ver", 1) or 1) < db_ver:
        return None

    payload["role"] = db_role or "user"
    return payload


# FastAPI Auth Dependencies
async def get_current_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    token = extract_session_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    payload = await _validate_session_against_db(decode_session_token(token), db=db)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid",
        )
    return payload


async def get_current_user_optional(
    request: Request,
    db: Optional[AsyncSession] = None,
) -> Optional[dict]:
    token = extract_session_token(request)
    if not token:
        return None
    return await _validate_session_against_db(decode_session_token(token), db=db)


def check_vault_unlocked(session_payload: dict) -> bool:
    if not session_payload or not session_payload.get("vault_unlocked"):
        return False
    vault_exp = session_payload.get("vault_exp", 0)
    return vault_exp > int(time.time())


async def require_vault_unlocked(
    session: dict = Depends(get_current_session),
) -> dict:
    if not check_vault_unlocked(session):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )
    return session


def require_role(required_role: str):
    async def _role_checker(session: dict = Depends(get_current_session)) -> dict:
        user_role = session.get("role", "user")
        if user_role != required_role and user_role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: requires '{required_role}' privileges.",
            )
        return session
    return _role_checker


require_admin = require_role("admin")

