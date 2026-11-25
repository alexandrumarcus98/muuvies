import pytest
import time
from backend.app.security import (
    hash_secret,
    verify_secret,
    encrypt_text,
    decrypt_text,
    create_session_token,
    decode_session_token,
    check_vault_unlocked,
    LoginRateLimiter,
)


def test_hash_and_verify_secret():
    pw = "super-secret-password-123"
    hashed = hash_secret(pw)
    assert hashed != pw
    assert verify_secret(pw, hashed) is True
    assert verify_secret("wrong-password", hashed) is False


def test_fernet_encryption_and_decryption():
    plain = "my-gemini-api-key-test-123"
    cipher = encrypt_text(plain)
    assert cipher != plain
    decrypted = decrypt_text(cipher)
    assert decrypted == plain


def test_session_token_vault_state(monkeypatch):
    from backend.app.security import _user_token_version_floors

    monkeypatch.delitem(_user_token_version_floors, "admin", raising=False)

    # Locked session
    token_locked = create_session_token("admin", vault_unlocked=False)
    payload_locked = decode_session_token(token_locked)
    assert payload_locked is not None
    assert payload_locked["sub"] == "admin"
    assert check_vault_unlocked(payload_locked) is False

    # Unlocked session
    now = int(time.time())
    token_unlocked = create_session_token("admin", vault_unlocked=True, vault_exp=now + 1800)
    payload_unlocked = decode_session_token(token_unlocked)
    assert payload_unlocked is not None
    assert check_vault_unlocked(payload_unlocked) is True

    # Expired vault session
    token_expired_vault = create_session_token("admin", vault_unlocked=True, vault_exp=now - 10)
    payload_expired = decode_session_token(token_expired_vault)
    assert payload_expired is not None
    assert check_vault_unlocked(payload_expired) is False


def test_rate_limiter():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    ip = "192.168.1.100"
    ip = "192.0.2.1"

    assert limiter.is_rate_limited(ip)[0] is False
    limiter.record_failure(ip)
    limiter.record_failure(ip)
    assert limiter.is_rate_limited(ip)[0] is False

    limiter.record_failure(ip)
    limited, retry = limiter.is_rate_limited(ip)
    assert limited is True
    assert retry > 0

    limiter.reset(ip)
    assert limiter.is_rate_limited(ip)[0] is False

